"""
engine/build.py — 建仓逻辑（决策层）

新架构最终方案：
  - 建仓张数 = 交易所最大(max_size) × 安全系数（读交易所，不自算）
  - 环境校验（账户模式/持仓模式）必须走 SDK
  - 下单走 engine.trader 统一通道
"""
from __future__ import annotations
import logging
import time

from data.exchange import get_auth_client
from state import get_state, save_state
from diagnostic_logger import get_diag_logger
from formulas.safety import calc_safe_contracts

logger = logging.getLogger(__name__)


def _log(st, msg, level="INFO", cat="BUILD", data=None):
    diag = get_diag_logger()
    diag.log(level, cat, msg, data, mech="建仓")
    st.logs.append({"ts": time.strftime("%H:%M:%S"),
                    "msg": msg, "level": level, "cat": cat})
    if len(st.logs) > 500:
        st.logs = st.logs[-300:]


def _check_environment(st) -> tuple[bool, str]:
    """启动前硬性环境校验（账户模式 + 持仓模式）。返回 (是否通过, 错误信息)。"""
    client = get_auth_client()
    if client is None:
        return False, "请先配置API密钥"
    try:
        config = client.get_account_config()
        acct_lv = str(config.get("acctLv", ""))
        if acct_lv == "1":
            _log(st, "❌ 账户为现货模式，不支持合约")
            return False, "账户为现货模式，请在OKX切换为合约模式"
        if acct_lv == "4":
            _log(st, "❌ 账户为组合保证金，不支持")
            return False, "账户模式为组合保证金，不支持当前策略"
        pos_mode = client.get_position_mode()
        if pos_mode == "net_mode":
            r = client.set_position_mode("long_short_mode")
            if r.get("code", "") != "0":
                return False, "需手动设为双向持仓模式"
        _log(st, "✅ 环境校验通过")
        return True, ""
    except Exception as e:
        _log(st, f"⚠️ 环境校验异常: {e}", level="WARN")
        return False, f"环境校验失败: {e}"


def execute_start_grid(contracts: int = 0) -> dict:
    """执行建仓。contracts: 前端传入每边张数（0=用公式）。"""
    from engine.trader import batch_market_open
    from data.ticker import get_mark_px

    st = get_state()
    if st.running:
        return {"status": "error", "msg": "已在运行"}

    # 环境校验
    ok, err = _check_environment(st)
    if not ok:
        return {"status": "error", "msg": err}

    # 已有仓位 → 只恢复运行
    if st.position.long_contracts > 0 or st.position.short_contracts > 0:
        st.running = True
        st.pending_iceberg = 0
        # 认领交易所现有挂单，避免下轮 check_grid_tick 走破坏性撤单重挂；并回填网格统计
        try:
            from engine.grid import adopt_or_reset_grid_orders, backfill_grid_stats
            adopt_or_reset_grid_orders(st)
            backfill_grid_stats(st)
        except Exception as e:
            _log(st, f"⚠️ 恢复时认领/回填异常: {e}", level="WARN")
        _log(st, f"✅ 网格已恢复运行（多{st.position.long_contracts}/空{st.position.short_contracts}）")
        save_state()
        return {"status": "ok", "data": {"contracts": st.position.long_contracts, "resumed": True}}

    px = get_mark_px()
    if px <= 0:
        return {"status": "error", "msg": "无行情数据"}

    # 查交易所最大下单量
    client = get_auth_client()
    max_ct = 1
    if client:
        try:
            max_info = client.get_max_size(st.inst_id, leverage=st.leverage, px=px)
            max_buy = int(float(max_info.get("maxBuy", "0") or 0))
            max_sell = int(float(max_info.get("maxSell", "0") or 0))
            # 对冲双开：每边张数上限 = 交易所最大(单边) ÷ 2，资金分给双边
            max_ct = min(max_buy, max_sell) if max_buy > 0 and max_sell > 0 else max(max_buy, max_sell)
            max_ct = max(max_ct // 2, 1)
            if max_ct < 1:
                max_ct = 1
        except Exception as e:
            logger.warning(f"get_max_size失败: {e}")

    # 建仓张数：用户填写 > 0 用用户值；否则 = 交易所最大 × 安全系数
    if contracts > 0:
        init_c = contracts
    else:
        init_c = calc_safe_contracts(max_ct, st.safety_factor)
    if init_c < 1:
        init_c = 1
    if init_c > max_ct:
        _log(st, f"⚠️ 建仓{init_c}张超交易所最大{max_ct}，封顶")
        init_c = max_ct

    st.calc_details["position_calc"] = {
        "formula": "建仓 = 交易所最大 × 安全系数",
        "vars": f"交易所最大={max_ct}张, 安全系数={st.safety_factor}, 用户填写={contracts}张",
        "result": f"建仓={init_c}张/边",
    }

    # 同步杠杆
    if client:
        try:
            lr = client.set_leverage(st.inst_id, st.leverage, "cross")
            if lr.get("code") == "0":
                _log(st, f"⚙️ 杠杆已同步: {st.inst_id} {st.leverage}x", cat="TRADE")
        except Exception as e:
            logger.warning(f"设置杠杆异常: {e}")

    # 下单
    _log(st, f"📤 [建仓] 向交易所下单: {st.inst_id} 多{init_c}+空{init_c}")
    result = batch_market_open(init_c)
    if result.get("status") != "ok":
        return result

    # 更新状态
    st.initial_contracts = init_c
    st.single_limit = init_c  # 单边极限 = 每边张数（已÷2）
    # 本金基准 = 建仓时刻总权益（含预留）
    # 止盈公式 profit_ratio=(当前总权益-本金基准)/本金基准：预留的1000不算盈利，须真赚够1%才全平
    st.capital = max(st.total_equity, 1)  # 本金基准 = 建仓时总权益(含预留)，止盈按此口径
    _log(st, f"💰 本金基准锁定: 总权益 {st.capital:.2f} USDT"
             f"（预留{st.reserved_capital:.2f}，网格可用{st.capital - st.reserved_capital:.2f}）", cat="PARAM")
    st.position.long_contracts = init_c
    st.position.short_contracts = init_c
    st.position.long_avg_px = px
    st.position.short_avg_px = px
    st.position.mark_px = px
    st.running = True
    st.grid_count = 0
    st.adjust_records = []
    st.adjust_seen_ord_ids = []
    st.build_ts = time.time()
    # 批次2：建仓 = 新一轮起点 → 重置高水位净浮盈 + 解除熔断禁重建（人工重启允许重建）
    st.peak_upl = 0.0
    st.auto_rebuild_blocked = False
    st.flat_reason = ""
    st.flat_ts = 0.0
    save_state()
    return {"status": "ok", "data": {"contracts": init_c, "px": px}}
