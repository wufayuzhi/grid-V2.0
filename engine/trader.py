"""
engine/trader.py — 统一下单通道（决策层执行器）

新架构：engine 决策层不直接调 raw_rest_client 下单，
统一走本模块。本模块封装建仓/回补/全平/撤单等交易操作。

职责：把决策结果翻译成 OKX 下单调用（走 data.exchange 的认证客户端），
记录订单、处理失败、更新 state。
"""
from __future__ import annotations
import logging
import time

from data.exchange import get_auth_client, sync_positions
from state import get_state, save_state
from diagnostic_logger import get_diag_logger

logger = logging.getLogger(__name__)


def _log(st, msg, level="INFO", cat="TRADE", data=None):
    diag = get_diag_logger()
    diag.log(level, cat, msg, data, mech="交易")
    st.logs.append({"ts": time.strftime("%H:%M:%S"),
                    "msg": msg, "level": level, "cat": cat})
    if len(st.logs) > 500:
        st.logs = st.logs[-300:]


def batch_market_open(contracts: int) -> dict:
    """建仓：多空各开 contracts 张市价单。返回 {"status": "ok"/"error", ...}。"""
    st = get_state()
    client = get_auth_client()
    if client is None:
        return {"status": "error", "msg": "认证客户端未就绪"}
    inst = st.inst_id
    orders = [
        {"instId": inst, "tdMode": "cross", "side": "buy",
         "posSide": "long", "ordType": "market", "sz": str(contracts)},
        {"instId": inst, "tdMode": "cross", "side": "sell",
         "posSide": "short", "ordType": "market", "sz": str(contracts)},
    ]
    try:
        result = client.batch_orders(orders)
    except Exception as e:
        _log(st, f"❌ 建仓下单异常: {e}", level="ERROR")
        return {"status": "error", "msg": f"下单失败: {e}"}

    if not result or result.get("code") not in ("0", None):
        _log(st, f"❌ 建仓被拒: {result}", level="ERROR")
        return {"status": "error", "msg": f"下单失败: {result.get('msg', '') if result else '无响应'}"}

    sub_orders = result.get("data", [])
    failures = [f"#{i+1} {so.get('sCode','')} {so.get('sMsg','')}"
                for i, so in enumerate(sub_orders) if so.get("sCode", "0") != "0"]
    if failures:
        _log(st, f"⚠️ 部分建仓失败: {'; '.join(failures)}", level="WARN")
        # 平掉已成交侧，避免单边
        for i, so in enumerate(sub_orders):
            if so.get("sCode") == "0":
                side = "long" if i == 0 else "short"
                try:
                    client.close_position(inst, side)
                    _log(st, f"↩️ 平掉已成交{side}侧")
                except Exception as ce:
                    _log(st, f"⚠️ 平{side}失败: {ce}", level="WARN")
        return {"status": "error", "msg": f"部分建仓失败: {'; '.join(failures)}"}

    _log(st, f"✅ 建仓成功: {inst} 多{contracts}张/空{contracts}张",
         data={"contracts": contracts, "ord_ids": [so.get("ordId") for so in sub_orders]})
    return {"status": "ok", "data": {"contracts": contracts}}


def reduce_position(side: str, contracts: int) -> dict:
    """部分减仓（回补用）：市价平掉指定方向 N 张，另一侧不动。

    side: long/short（要减的重仓侧）。contracts: 减几张。
    用 batch_orders 市价 reduce-only 单（双向持仓模式下 posSide 指定方向）。
    """
    st = get_state()
    if contracts <= 0:
        return {"status": "error", "msg": "减仓张数必须>0"}
    client = get_auth_client()
    if client is None:
        return {"status": "error", "msg": "认证客户端未就绪"}
    inst = st.inst_id
    # 减多 = sell posSide=long；减空 = buy posSide=short
    side_map = {"long": ("sell", "long"), "short": ("buy", "short")}
    if side not in side_map:
        return {"status": "error", "msg": f"未知方向 {side}"}
    s, ps = side_map[side]
    order = {"instId": inst, "tdMode": "cross", "side": s,
             "posSide": ps, "ordType": "market", "sz": str(contracts)}
    try:
        result = client.batch_orders([order])
    except Exception as e:
        _log(st, f"❌ 减仓下单异常: {e}", level="ERROR")
        return {"status": "error", "msg": f"减仓失败: {e}"}
    if not result or result.get("code") not in ("0", None):
        _log(st, f"❌ 减仓被拒: {result}", level="ERROR")
        return {"status": "error", "msg": f"减仓失败: {result.get('msg','') if result else '无响应'}"}
    sub = (result.get("data") or [{}])[0]
    if sub.get("sCode", "0") != "0":
        _log(st, f"❌ 减仓{side}{contracts}张被拒: {sub.get('sMsg','')}", level="ERROR")
        return {"status": "error", "msg": sub.get("sMsg", "减仓被拒")}
    _log(st, f"🔻 回补: 减{side} {contracts}张", cat="REBAL",
         data={"side": side, "contracts": contracts, "ord_id": sub.get("ordId")})
    sync_positions()
    return {"status": "ok"}


def rebalance_batch(heavy: str, n: int, use_limit: bool = True) -> dict:
    """分批回补·双向对开执行器（2026-08-15 定稿）。

    heavy: 重仓侧 long/short；n: 每批张数（双向各半，平重仓n + 开轻仓n）。
    总持仓不变：平重仓n + 开轻仓n，多空更均衡。
    use_limit=True → 挂限价贴近盘口（买→买一bid，卖→卖一ask），maker秒成；
    use_limit=False → 市价兜底（限价超时转市价时用）。
    返回 {"status":"ok"/"error", ...}。
    """
    st = get_state()
    if n <= 0:
        return {"status": "error", "msg": "回补张数必须>0"}
    client = get_auth_client()
    if client is None:
        return {"status": "error", "msg": "认证客户端未就绪"}
    inst = st.inst_id

    # 盘口价：优先用行情缓存 bid/ask，回退 mark
    _px = {"bid": st.position.mark_px, "ask": st.position.mark_px}
    try:
        from data.ticker import get_ticker_cache
        for e in get_ticker_cache():
            if e.get("inst_id") == inst:
                _px["bid"] = e.get("bid_px") or e.get("mark_px") or _px["bid"]
                _px["ask"] = e.get("ask_px") or e.get("mark_px") or _px["ask"]
                break
    except Exception:
        pass
    bid_px, ask_px = _px["bid"], _px["ask"]

    if heavy == "short":
        # 重仓空 → 平空(买) + 开多(买)，双向都是 buy，买一价
        orders = [
            {"instId": inst, "tdMode": "cross", "side": "buy", "posSide": "short",
             "ordType": "limit" if use_limit else "market", "sz": str(n),
             "px": str(bid_px or 0) if use_limit else None},
            {"instId": inst, "tdMode": "cross", "side": "buy", "posSide": "long",
             "ordType": "limit" if use_limit else "market", "sz": str(n),
             "px": str(bid_px or 0) if use_limit else None},
        ]
    else:
        # 重仓多 → 平多(卖) + 开空(卖)，双向都是 sell，卖一价
        orders = [
            {"instId": inst, "tdMode": "cross", "side": "sell", "posSide": "long",
             "ordType": "limit" if use_limit else "market", "sz": str(n),
             "px": str(ask_px or 0) if use_limit else None},
            {"instId": inst, "tdMode": "cross", "side": "sell", "posSide": "short",
             "ordType": "limit" if use_limit else "market", "sz": str(n),
             "px": str(ask_px or 0) if use_limit else None},
        ]
    # market 单去掉 px 字段
    if not use_limit:
        for o in orders:
            o.pop("px", None)

    try:
        result = client.batch_orders(orders)
    except Exception as e:
        _log(st, f"❌ 回补双向对开异常: {e}", level="ERROR")
        return {"status": "error", "msg": f"回补失败: {e}"}
    if not result or result.get("code") not in ("0", None):
        _log(st, f"❌ 回补双向对开被拒: {result}", level="ERROR")
        return {"status": "error", "msg": f"回补失败: {result.get('msg','') if result else '无响应'}"}
    subs = result.get("data", [])
    failures = [f"{so.get('sCode','')}:{so.get('sMsg','')}" for so in subs if so.get("sCode", "0") != "0"]
    if failures:
        _log(st, f"⚠️ 回补部分失败: {'; '.join(failures)}", level="WARN")
        return {"status": "error", "msg": f"回补部分失败: {'; '.join(failures)}"}
    mode = "限价" if use_limit else "市价"
    # 记录本次回补单 ordId（限价单超时转市价前需先撤；市价单无需撤）
    if use_limit:
        st._rebalance_pending_ord_ids = [so.get("ordId", "") for so in subs if so.get("ordId")]
    else:
        st._rebalance_pending_ord_ids = []
    _log(st, f"🔄 回补({mode}): {heavy}侧双向对开 平{heavy}{n}+开轻仓{n}",
         cat="REBAL", data={"heavy": heavy, "n": n, "mode": mode,
                            "ord_ids": st._rebalance_pending_ord_ids})
    sync_positions()
    return {"status": "ok"}


def close_side(side: str, contracts: int) -> dict:
    """平单侧仓位（side: long/short），用于回补/缩仓/全平。"""
    st = get_state()
    client = get_auth_client()
    if client is None:
        return {"status": "error", "msg": "认证客户端未就绪"}
    inst = st.inst_id
    # 平多 = close_position(inst, "long")；平空 = close_position(inst, "short")
    try:
        result = client.close_position(inst, side)
    except Exception as e:
        _log(st, f"❌ 平{side}异常: {e}", level="ERROR")
        return {"status": "error", "msg": f"平仓失败: {e}"}
    if not result or result.get("code") not in ("0", None):
        _log(st, f"❌ 平{side}被拒: {result}", level="ERROR")
        return {"status": "error", "msg": f"平仓失败: {result.get('msg','') if result else '无响应'}"}
    _log(st, f"✅ 平{side}完成", data={"side": side, "contracts": contracts})
    sync_positions()
    return {"status": "ok"}


def flat_all() -> dict:
    """全平：多空两侧全部市价平仓。"""
    st = get_state()
    client = get_auth_client()
    if client is None:
        return {"status": "error", "msg": "认证客户端未就绪"}
    inst = st.inst_id
    errs = []
    for side in ("long", "short"):
        try:
            r = client.close_position(inst, side)
            if not r or r.get("code") not in ("0", None):
                errs.append(f"{side}:{r.get('msg','') if r else '无响应'}")
        except Exception as e:
            errs.append(f"{side}:{e}")
    if errs:
        _log(st, f"⚠️ 全平部分失败: {'; '.join(errs)}", level="WARN")
        return {"status": "error", "msg": f"全平失败: {'; '.join(errs)}"}
    _log(st, "✅ 全平完成", cat="FLAT")
    sync_positions()
    st.running = False
    save_state()
    return {"status": "ok"}
