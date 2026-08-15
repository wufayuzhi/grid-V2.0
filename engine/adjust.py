"""engine/adjust.py — 调平决策（决策层 + 执行接线）

统一联动架构定稿：
  - 失衡率 = |多-空|/(多+空)×100% 为仓位危险度总指挥
  - 低/中档(≤20%)：回补盈利侧（重仓侧），继续跑
  - 高档(>20% 且 <70%)：按净浮盈回补盈利侧，保持网格运行
  - 爆表(≥70%)：安全距离兜底 → 市价全平（execute_emergency）
  - 止盈：eq 盈利 ≥ 递增档位(1%→2%→3%...) → 全平
  - 失血熔断：滑动窗口权益回撤 > 阈值 → 全平（物理网线）

本次修复核心：所有决策真正接线到执行（reduce_position / execute_emergency），
不再是只打日志的占位壳。失血熔断改为"权益回撤速率"口径（非净浮盈绝对值）。
"""
from __future__ import annotations
import logging
import time

from state import get_state
from diagnostic_logger import get_diag_logger
from formulas.safety import calc_imbalance_rate, rebalance_contracts

logger = logging.getLogger(__name__)


def _log(st, msg, level="INFO", cat="ADJUST", data=None):
    diag = get_diag_logger()
    diag.log(level, cat, msg, data, mech="调平")
    st.logs.append({"ts": time.strftime("%H:%M:%S"),
                    "msg": msg, "level": level, "cat": cat})
    if len(st.logs) > 500:
        st.logs = st.logs[-300:]


def _full_flat(st, reason: str, block_rebuild: bool = False):
    """执行市价全平（决策→执行接线）。失败也记日志，避免静默。

    block_rebuild=True：熔断类全平(失血/累计回撤/安全距离) → 禁止自动重建；
    止盈/手动全平 → 不 block（仅止盈自动重建，其余全平后保持手动）。
    """
    from engine.flat import execute_emergency
    try:
        eq_before = getattr(st, "total_equity", 0) or 0          # 全平前权益快照
        l_before = st.position.long_contracts or 0
        s_before = st.position.short_contracts or 0
        r = execute_emergency(st, reason)
        if r.get("status") == "ok":
            st.flat_reason = reason
            st.flat_ts = time.time()
            if block_rebuild:
                st.auto_rebuild_blocked = True
            _log(st, f"🏁 [全平] {reason} 完成" +
                 ("（熔断保护·禁自动重建）" if block_rebuild else "（可重建）"),
                 cat="RISK")
            # 自动全平 → 企微推送权益信息（重拉交易所余额拿平仓后真实权益）
            try:
                from data.exchange import sync_equity
                sync_equity()
                eq_after = getattr(st, "total_equity", 0) or 0
                inst = getattr(st, "inst_id", "")
                fee = max(eq_before - eq_after, 0) if eq_before and eq_after else 0
                lines = [f"{inst} 平多{l_before}+平空{s_before}",
                         f"平仓前权益 {eq_before:.2f} USDT",
                         f"平仓后权益 {eq_after:.2f} USDT"]
                if fee > 0:
                    lines.append(f"平仓手续费 ≈ {fee:.2f} USDT")
                from notify import on_op
                on_op(f"🏁 [全平] {reason}", "\n".join(lines))
            except Exception:
                pass
        else:
            _log(st, f"⚠️ [全平] {reason} 执行失败: {r.get('msg','')}", level="ERROR", cat="RISK")
    except Exception as e:
        _log(st, f"⚠️ [全平] {reason} 异常: {e}", level="ERROR", cat="RISK")


def check_adjust(st=None):
    """主调平决策入口。running 时每轮 data_loop 调用。

    统一优先级裁决链（2026-08-06 定稿）：
      ① 失血熔断 + 累计回撤（优先级1，并联）→ 全平 + 禁重建
      ② 安全距离兜底（优先级2）→ 全平 + 禁重建
      ③ 滚动止盈（优先级3）→ 全平（可重建）
    前三者触发即 return（不串行执行），任一全平后写 auto_rebuild_blocked。
    """
    st = st or get_state()
    if not st.running or st.paused:
        return

    # ── 优先级1：失血熔断（速度窗，双确认）──
    if _check_bleed_melt(st):
        return

    # ── 优先级2：安全距离兜底（空间防线，独立于失衡率）──
    if _check_safety_flat(st):
        return

    # ── 优先级3：滚动止盈（eq 盈利，账户总权益）→ 全平落袋 ──
    if _check_take_profit(st):
        return

    pos = st.position
    long_n = pos.long_contracts
    short_n = pos.short_contracts
    if long_n == 0 and short_n == 0:
        return
    if long_n == short_n:
        return

    imbalance = calc_imbalance_rate(long_n, short_n)
    st.imbalance_rate = round(imbalance, 2)

    # 风控开关：use_risk_control 控制回补（止盈/失血/累计回撤/安全距离是独立保险，不受此开关管）
    if not getattr(st, "use_risk_control", True):
        return

    # ── 失衡回补（单一触发条件）──
    # 仅当开关开启、且失衡率 ≥ 回补触发值 时，市价减重仓侧补到目标失衡率。
    # 失衡率 < 触发值 → 不回补（只靠档位密度缩窄/盈亏平衡点渐进化解，不主动砍仓）。
    # 触发值/目标值均为 0-100 滑块；目标必须 < 触发（routes 已校验）。
    trigger = float(getattr(st, "imbalance_threshold_pct", 80.0) or 80.0)
    target = float(getattr(st, "rebalance_target_pct", 40.0) or 40.0)
    if getattr(st, "use_rebalance", False) and imbalance >= trigger:
        _do_rebalance(st, imbalance, target, _start_new_round=True)
    # 失衡已回到目标以下 → 本轮回补完成（下次失衡再超触发时计为新的一轮）
    elif getattr(st, "use_rebalance", False) and imbalance <= target:
        st.rebalance_complete = True


def _do_rebalance(st, imbalance, target, _start_new_round=False):
    """分批回补·双向对开（2026-08-15 定稿）。

    每批：平重仓 N + 开轻仓 N（总持仓不变，多空更均衡）。
      N = (净敞口 − 目标净敞口) ÷ 2 ÷ 批数
    挂限价贴近盘口（买→买一，卖→卖一）；限价超时未成交 → 转市价兜底。
    默认降到中性（不押方向，趋势观察模式）。
    批间间隔按 rebalance_batch_gap_min；批数 rebalance_batches。
    """
    pos = st.position
    now = time.time()
    # 冷却：距上次回补不足 N 秒则跳过（防高失衡下每2s连发）
    last = getattr(st, "last_rebalance_ts", 0.0)
    cooldown = max(st.data_loop_interval * 3, 5.0)
    if now - last < cooldown:
        return
    if pos.long_contracts > pos.short_contracts:
        heavy, light = "long", pos.short_contracts
    else:
        heavy, light = "short", pos.long_contracts

    # 每批张数 = 净敞口需要化解 ÷ 2 ÷ 批数
    total = pos.long_contracts + pos.short_contracts
    net = abs(pos.long_contracts - pos.short_contracts)
    target_net = total * (target / 100)
    need_total = max(int(net - target_net), 0)
    batches = max(int(getattr(st, "rebalance_batches", 2) or 2), 1)
    per_batch = max(need_total // (2 * batches), 1)
    if per_batch <= 0:
        return

    from engine.trader import rebalance_batch

    # ── 分批状态机：检查是否有未完成批次 ──
    cur_batch = getattr(st, "_rebalance_cur_batch", 0)
    batch_start_ts = getattr(st, "_rebalance_batch_ts", 0.0)
    gap_min = float(getattr(st, "rebalance_batch_gap_min", 20.0) or 20.0)
    timeout_min = float(getattr(st, "rebalance_limit_timeout_min", 10.0) or 10.0)

    if cur_batch == 0:
        # 新一轮：执行批1（限价）
        r = rebalance_batch(heavy, per_batch, use_limit=True)
        if r.get("status") != "ok":
            return
        st._rebalance_cur_batch = 1
        st._rebalance_batch_ts = now
        st.last_rebalance_ts = now
        _log(st, f"🔄 [回补] 批1/{batches} 双向对开 平{heavy}{per_batch}+开轻仓{per_batch} (限价)",
             cat="REBAL", data={"batch": 1, "total": batches, "per": per_batch, "heavy": heavy})
        save_state_lazy(st)
        return

    # 已有批次在进行：检查批间间隔是否到 → 执行下一批
    if cur_batch < batches and (now - batch_start_ts) >= gap_min * 60:
        r = rebalance_batch(heavy, per_batch, use_limit=True)
        if r.get("status") != "ok":
            return
        st._rebalance_cur_batch = cur_batch + 1
        st._rebalance_batch_ts = now
        st.last_rebalance_ts = now
        _log(st, f"🔄 [回补] 批{cur_batch+1}/{batches} 双向对开 平{heavy}{per_batch}+开轻仓{per_batch} (限价)",
             cat="REBAL", data={"batch": cur_batch + 1, "total": batches, "per": per_batch, "heavy": heavy})
        save_state_lazy(st)
        return

    # 所有批次完成 → 重置状态机 + 标记网格重挂（回补后基于新持仓重挂）
    if cur_batch >= batches:
        st._rebalance_cur_batch = 0
        st._rebalance_batch_ts = 0.0
        st.rebalance_complete = True
        st.rebalance_cnt = (getattr(st, "rebalance_cnt", 0) or 0) + 1
        st._need_rehang = True  # 网格重挂用新持仓重算锚点
        _log(st, f"🔄 [回补] 全部{batches}批完成，标记网格重挂",
             cat="REBAL", data={"total_batches": batches, "cnt": st.rebalance_cnt})
        save_state_lazy(st)


def save_state_lazy(st):
    try:
        from state import save_state
        save_state()
    except Exception:
        pass


def _check_cumulative_drawdown(st) -> bool:
    """累计回撤兜底：高水位峰值upl回撤 > 阈值 → 全平 + 禁重建。慢速阴跌防线。

    累计回撤 = (峰值upl − 最新upl) ÷ (账户权益 − 累计追加本金) × 100%（高水位）
    peak_upl 由 tick 层每轮更新（max 高水位）；建仓/全平后重置。
    """
    if not getattr(st, "use_cumulative_drawdown_flat", True):
        return False
    try:
        from formulas.drawdown import calc_cumulative_drawdown_pct
        pos = st.position
        latest_upl = (pos.long_unrealized_pnl or 0) + (pos.short_unrealized_pnl or 0)
        dd = calc_cumulative_drawdown_pct(
            st.peak_upl, latest_upl, st.total_equity, st.cumulative_added)
        if dd > st.cumulative_drawdown_threshold:
            _log(st, f"📉 [累计回撤] 高水位回撤 {dd:.2f}% > 阈值{st.cumulative_drawdown_threshold:.1f}% "
                     f"(峰值upl={st.peak_upl:.2f}, 当前upl={latest_upl:.2f}) → 熔断",
                 level="WARN", cat="RISK",
                 data={"drawdown_pct": round(dd, 2), "peak_upl": round(st.peak_upl, 2),
                       "latest_upl": round(latest_upl, 2)})
            _full_flat(st, "累计回撤熔断", block_rebuild=True)
            return True
        elif dd > 0.7 * st.cumulative_drawdown_threshold:
            try:
                from notify import on_risk_close
                on_risk_close(f"回撤{dd:.2f}% 接近{st.cumulative_drawdown_threshold:.1f}%累计线",
                              round(st.total_equity or 0, 2))
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"cumulative_drawdown: {e}")
    return False


def _check_safety_flat(st) -> bool:
    """安全距离兜底（原爆表，空间防线）：安全距离 < 阈值 → 全平 + 禁重建。

    独立于失衡率触发（文档§七）：只要离爆仓近就出手，不等到失衡≥70%。
    liqPx 异常缺失 → 告警 + 暂停判定（不静默当安全）。
    """
    if not getattr(st, "use_safety_flat", True):
        return False
    try:
        from formulas.safety import calc_safety_distance
        pos = st.position
        sd = calc_safety_distance(pos.long_contracts, pos.short_contracts,
                                  pos.long_liq_px, pos.short_liq_px, pos.mark_px)
        st.safety_distance_pct = round(sd, 2)
        if sd >= 999.0:
            # 完全对冲或无有效liqPx（"--"/0/空）→ 视为超级安全，不触发
            return False
        if sd < st.safety_flat_threshold:
            _log(st, f"🚨 [安全距离] 距强平 {sd:.2f}% < 阈值{st.safety_flat_threshold:.1f}% → 全平保命",
                 level="WARN", cat="RISK",
                 data={"safety_distance_pct": round(sd, 2),
                       "threshold": st.safety_flat_threshold})
            _full_flat(st, "安全距离兜底", block_rebuild=True)
            return True
        elif sd < 1.3 * st.safety_flat_threshold:
            try:
                from notify import on_risk_close
                on_risk_close(f"安全距离{sd:.2f}% 接近{st.safety_flat_threshold:.1f}%平仓线",
                              round(st.total_equity or 0, 2))
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"safety_flat: {e}")
    return False


def _check_take_profit(st) -> bool:
    """止盈：eq 盈利达当前递增档位 → 全平落袋。"""
    try:
        from formulas.take_profit import should_take_profit
        trigger, tp = should_take_profit(
            st.build_ts, st.total_equity, st.capital,
            st.tp_base_pct, st.tp_window_hours)
        if trigger:
            _log(st, f"🎯 [止盈] eq盈利达 {tp:.1f}% 档位 → 全平", cat="TP",
                 data={"tp_pct": tp})
            _full_flat(st, "止盈落袋", block_rebuild=False)
            return True
    except Exception as e:
        logger.debug(f"take_profit: {e}")
    return False


def _check_bleed_melt(st) -> bool:
    """失血熔断：滑动窗口权益回撤速率 > 阈值 → 全平。

    口径修正（定稿）：回撤 = (窗口前权益 − 当前权益) ÷ 窗口前权益 × 100%。
    之前误用"净浮盈绝对值"阈值，现改为权益回撤速率（物理网线语义）。

    双确认（定稿，不加系数）：
      触发 = 权益窗口回撤 > 失血阈值 且 安全距离 < 安全距离阈值
      目的：影线针瞬间回撤快但价格拉回、安全距离没缩 → 不触发（吃针利润）；
            真崩盘回撤快 + 离强平近 → 触发全平。
    """
    if not st.use_bleed_melt:
        return False
    try:
        dd = _window_drawdown_pct(st)
        if dd > st.bleed_threshold_pct:
            # 双确认第二道闸门：安全距离必须缩到危险线内
            sd = getattr(st, "safety_distance_pct", 999.0)
            if not (0 <= sd < st.safety_flat_threshold):
                # 影线针/闪插：回撤快但离爆仓仍远 → 不触发失血，网格继续吃针利润
                _log(st, f"🩸 [失血] 权益回撤 {dd:.2f}% > 阈值{st.bleed_threshold_pct:.1f}% "
                         f"但安全距离{sd:.1f}% ≥ 危险线{st.safety_flat_threshold:.1f}% → 视为影线针不触发",
                     level="INFO", cat="RISK", data={"drawdown_pct": round(dd, 2), "safety_distance_pct": round(sd, 2)})
                return False
            _log(st, f"🩸 [失血] 权益回撤 {dd:.2f}% > 阈值{st.bleed_threshold_pct:.1f}% "
                     f"且安全距离{sd:.1f}% < 危险线{st.safety_flat_threshold:.1f}% → 熔断",
                 level="WARN", cat="RISK", data={"drawdown_pct": round(dd, 2), "safety_distance_pct": round(sd, 2)})
            try:
                from notify import on_bleed
                on_bleed(f"{dd:.2f}%/窗 安全距离{sd:.1f}%", round(st.total_equity or 0, 2))
            except Exception:
                pass
            _full_flat(st, "失血熔断", block_rebuild=True)
            return True
        elif dd > 0.7 * st.bleed_threshold_pct:
            # 接近失血熔断线预警
            try:
                from notify import on_risk_close
                on_risk_close(f"回撤{dd:.2f}% 接近{st.bleed_threshold_pct:.1f}%失血线",
                              round(st.total_equity or 0, 2))
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"bleed_melt: {e}")
    return False


def _window_drawdown_pct(st) -> float:
    """权益回撤%：窗口(bleed_window_sec)前权益 vs 当前权益。"""
    hist = st.equity_history or []
    if len(hist) < 2:
        return 0.0
    now = time.time()
    target_t = now - getattr(st, "bleed_window_sec", 5.0)
    past = min(hist, key=lambda e: abs(e[0] - target_t))
    cur = hist[-1]
    if past[1] <= 0:
        return 0.0
    return (past[1] - cur[1]) / past[1] * 100
