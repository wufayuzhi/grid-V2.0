"""
engine/flat.py — 全平（决策层执行器）

流程：撤冰山单 → 撤限价单 → 等1秒 → 市价双向平仓 → 清本地状态。
走 data.exchange 认证客户端 + engine.trader 通道，不直接碰 SDK。
"""
from __future__ import annotations
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from state import get_state, save_state
from diagnostic_logger import get_diag_logger
from data.exchange import get_auth_client

logger = logging.getLogger(__name__)


def _log(st, msg, level="INFO", cat="FLAT", data=None):
    diag = get_diag_logger()
    diag.log(level, cat, msg, data, mech="全平")
    st.logs.append({"ts": time.strftime("%H:%M:%S"),
                    "msg": msg, "level": level, "cat": cat})
    if len(st.logs) > 500:
        st.logs = st.logs[-300:]


def execute_emergency(st=None, reason: str = "手动全平") -> dict:
    """市价全平（调真实API + 清本地状态）。返回 {"status": "ok"/"error", ...}。"""
    st = st or get_state()
    st.running = False
    pos = st.position
    total = pos.long_contracts + pos.short_contracts
    diag = get_diag_logger()
    diag.error("TRADE",
        f"全平: {reason}, 多={pos.long_contracts}张 空={pos.short_contracts}张 总计={total}张",
        {"reason": reason, "long": pos.long_contracts, "short": pos.short_contracts,
         "total": total, "mark_px": pos.mark_px,
         "long_upl": round(pos.long_unrealized_pnl, 2),
         "short_upl": round(pos.short_unrealized_pnl, 2)})

    client = get_auth_client()
    if total > 0:
        _log(st, f"🔴 [全平] {reason}，平{total}张", level="ERROR")
        if client is None:
            return {"status": "error", "msg": "认证客户端未就绪，无法全平"}
        # 1. 先撤所有挂单（冰山+普通限价），否则 close_position 报 51115
        try:
            r = client.cancel_all_algos(st.inst_id)
            if r.get("code") != "0":
                _log(st, f"⚠️ 撤冰山单失败: {r.get('msg','')[:60]}", level="WARN")
        except Exception as e:
            _log(st, f"⚠️ 撤冰山单异常: {e}", level="WARN")
        try:
            r = client.cancel_all_pending(st.inst_id)
            code = r.get("code", "?")
            if code == "0":
                n = r.get("msg", "").split("/")[1] if "/" in r.get("msg", "") else "0"
                _log(st, f"🧹 撤限价单: {n}笔")
            else:
                _log(st, f"⚠️ 撤限价单失败: code={code} {r.get('msg','')[:60]}", level="WARN")
        except Exception as e:
            _log(st, f"⚠️ 撤限价单异常: {e}", level="WARN")
        # 2. 撤单后稍等
        time.sleep(1)

        # 3. 市价双向平仓（并行）
        def close_side(ps: str):
            try:
                return client.close_position(st.inst_id, ps)
            except Exception as e:
                return {"code": "-1", "msg": str(e)}

        tasks = []
        if pos.long_contracts > 0:
            tasks.append("long")
        if pos.short_contracts > 0:
            tasks.append("short")
        closed = set()
        if tasks:
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                futures = {pool.submit(close_side, ps): ps for ps in tasks}
                for fut in as_completed(futures):
                    ps = futures[fut]
                    try:
                        r = fut.result()
                        code = r.get('code', '?')
                        if code == '0':
                            closed.add(ps)
                            _log(st, f"  ✅ 平{ps}成功")
                        else:
                            _log(st, f"  ❌ 平{ps}失败: code={code} {r.get('msg','')[:60]}",
                                 level="ERROR")
                    except Exception as e:
                        _log(st, f"  ⚠️ 平{ps}异常: {e}", level="WARN")
        # 全平必须"所有需要平的仓位都成功"才算成功（修复：一侧失败仍报"全平完成"的安全误报）
        if closed != set(tasks):
            failed = sorted(set(tasks) - closed)
            _log(st, f"🚨 [全平] 部分失败: {','.join(failed)} 侧未平掉 → 返回失败(勿当全平成功)",
                 level="ERROR", cat="FLAT")
            save_state()
            return {"status": "error",
                    "msg": f"全平未完成: {','.join(failed)} 侧平仓失败，账户仍留有敞口"}
        # 只清零已成功平仓的仓位
        if 'long' in closed:
            pos.long_contracts = 0
            pos.long_avg_px = 0
            pos.long_liq_px = 0
            pos.long_be_px = 0
        if 'short' in closed:
            pos.short_contracts = 0
            pos.short_avg_px = 0
            pos.short_liq_px = 0
            pos.short_be_px = 0

    st.pending_iceberg = 0
    st.grid_upper_px = 0.0
    st.grid_lower_px = 0.0
    save_state()
    return {"status": "ok", "msg": "全平完成"}
