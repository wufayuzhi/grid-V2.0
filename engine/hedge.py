"""
engine/hedge.py — 对锁（决策层执行器）

失衡时增开弱势侧仓位，使净敞口归零（long == short）。
走 data.exchange 认证客户端 + engine.trader 通道。
"""
from __future__ import annotations
import time
import logging

from state import get_state, save_state
from diagnostic_logger import get_diag_logger
from data.exchange import get_auth_client

logger = logging.getLogger(__name__)


def _log(st, msg, level="INFO", cat="HEDGE", data=None):
    diag = get_diag_logger()
    diag.log(level, cat, msg, data, mech="对冲/冰山")
    st.logs.append({"ts": time.strftime("%H:%M:%S"),
                    "msg": msg, "level": level, "cat": cat})
    if len(st.logs) > 500:
        st.logs = st.logs[-300:]


def execute_hedge(st=None) -> dict:
    """执行对锁：增开弱势侧仓位使 long == short，净敞口归零。"""
    st = st or get_state()
    pos = st.position
    long_ct = pos.long_contracts
    short_ct = pos.short_contracts

    if long_ct <= 0 and short_ct <= 0:
        return {"status": "error", "msg": "无仓位可对锁"}
    if long_ct == short_ct:
        _log(st, "🔒 多空已平衡，无需对锁")
        return {"status": "ok", "balanced": True, "long": long_ct, "short": short_ct}

    client = get_auth_client()
    if client is None:
        return {"status": "error", "msg": "请先配置API密钥"}

    # 计算失衡：谁少加谁
    if long_ct > short_ct:
        diff = long_ct - short_ct
        pos_side = "short"
        order_side = "sell"
        action = f"开空{diff}张"
    else:
        diff = short_ct - long_ct
        pos_side = "long"
        order_side = "buy"
        action = f"开多{diff}张"

    # 查最大可开量，避免超限
    try:
        max_info = client.get_max_size(st.inst_id, leverage=st.leverage,
                                       px=pos.mark_px)
        max_buy = int(float(max_info.get("maxBuy", "0") or 0))
        max_sell = int(float(max_info.get("maxSell", "0") or 0))
    except Exception as e:
        logger.warning(f"对锁查询最大下单量失败: {e}")
        return {"status": "error", "msg": f"查询限额失败: {e}"}

    effective_max = max_sell if pos_side == "short" else max_buy
    if effective_max <= 0:
        return {"status": "error", "msg": "当前无可开额度"}

    sz = min(diff, effective_max)
    if sz <= 0:
        return {"status": "error", "msg": "计算对锁张数异常"}

    _log(st, f"🔐 [对锁] {action}（限额{effective_max}张）",
         data={"pos_side": pos_side, "sz": sz, "effective_max": effective_max})

    # 下单：开弱势侧（posSide + side），统一走 batch_orders
    try:
        result = client.batch_orders([{
            "instId": st.inst_id, "tdMode": "cross", "side": order_side,
            "posSide": pos_side, "ordType": "market", "sz": str(sz)}])
    except Exception as e:
        _log(st, f"❌ 对锁下单异常: {e}", level="ERROR")
        return {"status": "error", "msg": f"下单失败: {e}"}

    if not result or result.get("code") not in ("0", None):
        _log(st, f"❌ 对锁被拒: {result}", level="ERROR")
        return {"status": "error", "msg": f"下单失败: {result.get('msg','') if result else '无响应'}"}

    _log(st, f"✅ [对锁] {action} 成功")
    # 刷新持仓
    try:
        from data.exchange import sync_positions
        sync_positions()
    except Exception:
        pass
    save_state()
    return {"status": "ok", "msg": f"对锁成功: {action}"}
