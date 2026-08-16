"""
state.py — 网格状态管理（新架构：薄状态，不藏公式）
只存数据 + 持久化 + to_dict 契约（前端依赖）。
计算在 formulas/，读取在 data/，决策在 engine/。
"""
from __future__ import annotations
import time
import os
import json
from dataclasses import dataclass, field

from models import GridState, GridPosition
from formulas.price_precision import px_round  # 价格按交易所 tickSz 对齐


_STATE_FILE = os.environ.get("V2_STATE_FILE", "/app/data/v2_grid_state.json")

_state: GridState | None = None


def get_state() -> GridState:
    global _state
    if _state is None:
        _state = load_state()
    return _state


def save_state():
    st = get_state()
    data = {
        "inst_id": st.inst_id,
        "margin_mode": st.margin_mode,
        "simulated": st.simulated,
        "ct_val": st.ct_val,
        "running": st.running,
        "paused": st.paused,
        "leverage": st.leverage,
        "capital": st.capital,
        "total_equity": round(st.total_equity, 2) if st.total_equity is not None else None,
        "reserved_capital": st.reserved_capital,
        "use_rebalance": getattr(st, "use_rebalance", False),
        "total_pnl": round(st.total_pnl, 2),
        "total_fee": round(getattr(st, "total_fee", 0), 2),
        "grid_count": st.grid_count,
        "rebalance_cnt": getattr(st, "rebalance_cnt", 0),
        "long_contracts": st.position.long_contracts,
        "long_avg_px": px_round(st.inst_id, st.position.long_avg_px),
        "long_unrealized_pnl": round(st.position.long_unrealized_pnl, 2),
        "long_liq_px": px_round(st.inst_id, st.position.long_liq_px),
        "long_be_px": px_round(st.inst_id, st.position.long_be_px),
        "short_contracts": st.position.short_contracts,
        "short_avg_px": px_round(st.inst_id, st.position.short_avg_px),
        "short_unrealized_pnl": round(st.position.short_unrealized_pnl, 2),
        "short_liq_px": px_round(st.inst_id, st.position.short_liq_px),
        "short_be_px": px_round(st.inst_id, st.position.short_be_px),
        "position_margin": round(st.position.position_margin, 2),
        "notional_usd": round(st.position.notional_usd, 2),
        "mark_px": px_round(st.inst_id, st.position.mark_px),
        "last_px": px_round(st.inst_id, st.position.last_px),
        "atr_abs": st.atr_abs,
        "atr_pct": st.atr_pct,
        "atr_pct_prev": getattr(st, "atr_pct_prev", 0.0),
        "atr_last_push_ts": getattr(st, "atr_last_push_ts", 0.0),
        "atr_notify_change_pct": getattr(st, "atr_notify_change_pct", 30.0),
        "atr_notify_cooldown": getattr(st, "atr_notify_cooldown", 21600.0),
        "grid_upper_ord_ids": st.grid_upper_ord_ids[-20:],
        "grid_lower_ord_ids": st.grid_lower_ord_ids[-20:],
        "grid_placed_ts": getattr(st, "grid_placed_ts", 0.0),
        "grid_last_rehang_ts": getattr(st, "grid_last_rehang_ts", 0.0),
        "grid_rehang_hours": getattr(st, "grid_rehang_hours", 24.0),
        "grid_rehang_cooldown": getattr(st, "grid_rehang_cooldown", 3600.0),
        "grid_last_trade_ts": getattr(st, "grid_last_trade_ts", 0.0),
        "grid_12h_push_ts": getattr(st, "grid_12h_push_ts", 0.0),
        "last_rebalance_ts": getattr(st, "last_rebalance_ts", 0.0),
        "initial_contracts": st.initial_contracts,
        "single_limit": st.single_limit,
        "grid_upper_px": st.grid_upper_px,
        "grid_lower_px": st.grid_lower_px,
        "pending_iceberg": st.pending_iceberg,
        "tp_base_pct": st.tp_base_pct,
        "tp_window_hours": st.tp_window_hours,
        "imbalance_threshold_pct": st.imbalance_threshold_pct,
        "rebalance_target_pct": st.rebalance_target_pct,
        "rebalance_batches": st.rebalance_batches,
        "rebalance_batch_gap_min": st.rebalance_batch_gap_min,
        "rebalance_limit_timeout_min": st.rebalance_limit_timeout_min,
        "rebalance_market_after_timeout": st.rebalance_market_after_timeout,
        "safety_factor": st.safety_factor,
        "bleed_threshold_pct": st.bleed_threshold_pct,
        "target_spacing_pct": st.target_spacing_pct,
        "adj_ratio": st.adj_ratio,
        "price_offset_pct": st.price_offset_pct,
        "adjust_split_ratio": st.adjust_split_ratio,
        "base_density": st.base_density,
        "defense_density": st.defense_density,
        "mode": st.mode,
        "one_way_threshold": st.one_way_threshold,
        "loss_ratio_tight1": getattr(st, "loss_ratio_tight1", 5.0),
        "loss_ratio_tight2": getattr(st, "loss_ratio_tight2", 20.0),
        "ladder_rates": st.ladder_rates,
        "ladder_gap_up": st.ladder_gap_up,
        "ladder_gap_dn": st.ladder_gap_dn,
        "ladder_enabled": st.ladder_enabled,
        "ladder_exit_pct": getattr(st, "ladder_exit_pct", 15.0),
        "ladder_cooldown_min": getattr(st, "ladder_cooldown_min", 30.0),
        "confirm_time": st.confirm_time,
        "debounce_loss_line": st.debounce_loss_line,
        "debounce_profit_line": st.debounce_profit_line,
        "atr_timeframe": st.atr_timeframe,
        "atr_period": st.atr_period,
        "density_min": st.density_min,
        "cumulative_added": st.cumulative_added,
        "safety_flat_threshold": st.safety_flat_threshold,
        "auto_rebuild_blocked": st.auto_rebuild_blocked,
        "flat_reason": st.flat_reason,
        "flat_ts": st.flat_ts,
        # 批次3：单边趋势参数化
        "trend_tf": st.trend_tf,
        "ema_fast": st.ema_fast,
        "ema_slow": st.ema_slow,
        "st_period": st.st_period,
        "st_mult": st.st_mult,
        "iceberg_sz": st.iceberg_sz,
        "pxVar": st.pxVar,
        "auto_adjust": st.auto_adjust,
        "grid_auto_run": st.grid_auto_run,
        "use_iceberg": st.use_iceberg,
        "use_risk_control": st.use_risk_control,
        "use_bleed_melt": st.use_bleed_melt,
        "use_safety_flat": st.use_safety_flat,
        "use_dynamic_params": st.use_dynamic_params,
        "coin_amplitude_24h": st.coin_amplitude_24h,
        "amp_7d": st.amp_7d,
        "dr_24h": st.dr_24h,
        "dr_7d": st.dr_7d,
        "data_loop_interval": st.data_loop_interval,
        "oi_full_refresh_interval": st.oi_full_refresh_interval,
        "oi_history_size": st.oi_history_size,
        "oi_sample_count": st.oi_sample_count,
        "bleed_window_sec": st.bleed_window_sec,
        "equity_history_window_sec": st.equity_history_window_sec,
        "api_timeout": st.api_timeout,
        "health_stale_sec": st.health_stale_sec,
        "health_max_failures": st.health_max_failures,
        "equity_history": st.equity_history[-300:],
        "adjust_history": st.adjust_history[-100:],
        "logs": st.logs[-200:],
        "adjust_records": st.adjust_records[-300:],
        "adjust_seen_ord_ids": st.adjust_seen_ord_ids[-2000:],
        "build_ts": st.build_ts,
        "update_ts": time.time(),
    }
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        print(f"save_state: {e}")


def load_state() -> GridState:
    st = GridState()
    if not os.path.exists(_STATE_FILE):
        return st
    try:
        with open(_STATE_FILE) as f:
            data = json.load(f)
        st.inst_id = data.get("inst_id", st.inst_id)
        st.margin_mode = data.get("margin_mode", "usdt")
        st.simulated = data.get("simulated", False)
        st.running = data.get("running", False)
        st.paused = data.get("paused", False)
        st.ct_val = data.get("ct_val", 0.1)
        st.capital = data.get("capital", 1000)
        eq = data.get("total_equity")
        if eq is not None:
            st.total_equity = float(eq)
        st.reserved_capital = data.get("reserved_capital", 0)
        st.use_rebalance = data.get("use_rebalance", False)
        st.leverage = data.get("leverage", 20)
        st.total_pnl = data.get("total_pnl", 0)
        st.total_fee = data.get("total_fee", 0)
        st.grid_count = data.get("grid_count", 0)
        st.rebalance_cnt = data.get("rebalance_cnt", 0)
        st.position.long_contracts = data.get("long_contracts", 0)
        st.position.long_avg_px = data.get("long_avg_px", 0)
        st.position.long_unrealized_pnl = data.get("long_unrealized_pnl", 0)
        st.position.long_liq_px = data.get("long_liq_px", 0)
        st.position.long_be_px = data.get("long_be_px", 0)
        st.position.short_contracts = data.get("short_contracts", 0)
        st.position.short_avg_px = data.get("short_avg_px", 0)
        st.position.short_unrealized_pnl = data.get("short_unrealized_pnl", 0)
        st.position.short_liq_px = data.get("short_liq_px", 0)
        st.position.short_be_px = data.get("short_be_px", 0)
        st.position.mark_px = data.get("mark_px", 0)
        st.position.last_px = data.get("last_px", 0)
        st.atr_abs = data.get("atr_abs", 0)
        st.atr_pct = data.get("atr_pct", 0)
        st.atr_pct_prev = data.get("atr_pct_prev", 0.0)
        st.atr_last_push_ts = data.get("atr_last_push_ts", 0.0)
        st.atr_notify_change_pct = data.get("atr_notify_change_pct", 30.0)
        st.atr_notify_cooldown = data.get("atr_notify_cooldown", 21600.0)
        st.grid_upper_ord_ids = data.get("grid_upper_ord_ids", [])
        st.grid_lower_ord_ids = data.get("grid_lower_ord_ids", [])
        st.grid_placed_ts = data.get("grid_placed_ts", 0.0)
        st.grid_last_rehang_ts = data.get("grid_last_rehang_ts", 0.0)
        st.grid_rehang_hours = data.get("grid_rehang_hours", 24.0)
        st.grid_rehang_cooldown = data.get("grid_rehang_cooldown", 3600.0)
        st.grid_last_trade_ts = data.get("grid_last_trade_ts", 0.0)
        st.grid_12h_push_ts = data.get("grid_12h_push_ts", 0.0)
        st.last_rebalance_ts = data.get("last_rebalance_ts", 0.0)
        st.initial_contracts = data.get("initial_contracts", 0)
        st.single_limit = data.get("single_limit", 0)
        st.grid_upper_px = data.get("grid_upper_px", 0)
        st.grid_lower_px = data.get("grid_lower_px", 0)
        st.pending_iceberg = data.get("pending_iceberg", 0)
        st.tp_base_pct = data.get("tp_base_pct", 1.0)
        st.tp_window_hours = data.get("tp_window_hours", 24.0)
        st.imbalance_threshold_pct = data.get("imbalance_threshold_pct", 80.0)
        st.rebalance_target_pct = data.get("rebalance_target_pct", 40.0)
        st.rebalance_batches = data.get("rebalance_batches", 2)
        st.rebalance_batch_gap_min = data.get("rebalance_batch_gap_min", 20.0)
        st.rebalance_limit_timeout_min = data.get("rebalance_limit_timeout_min", 10.0)
        st.rebalance_market_after_timeout = data.get("rebalance_market_after_timeout", True)
        st.safety_factor = data.get("safety_factor", 0.7)
        st.bleed_threshold_pct = data.get("bleed_threshold_pct", 3.0)
        st.target_spacing_pct = data.get("target_spacing_pct", 0.60)
        st.adj_ratio = data.get("adj_ratio", 0.06)
        st.price_offset_pct = data.get("price_offset_pct", 0.2)
        st.adjust_split_ratio = data.get("adjust_split_ratio", 0.5)
        st.base_density = data.get("base_density", 2.0)
        st.defense_density = data.get("defense_density", 2.0)
        st.mode = data.get("mode", "attack")
        st.one_way_threshold = data.get("one_way_threshold", 60.0)
        st.loss_ratio_tight1 = data.get("loss_ratio_tight1", 5.0)
        st.loss_ratio_tight2 = data.get("loss_ratio_tight2", 20.0)
        st.ladder_rates = data.get("ladder_rates", [40, 50, 60, 70, 80])
        st.ladder_exit_pct = data.get("ladder_exit_pct", 15.0)
        st.ladder_cooldown_min = data.get("ladder_cooldown_min", 30.0)
        # 兼容旧仓：读新字段 gap_up/gap_dn；旧仓仅有 ladder_densities → 从旧密度换算初始价差
        _old_dens = data.get("ladder_densities")
        if data.get("ladder_gap_up") is not None:
            st.ladder_gap_up = data.get("ladder_gap_up")
        elif _old_dens is not None:
            st.ladder_gap_up = [max(round(d * 2.0, 2), 0.3) for d in _old_dens]
        else:
            st.ladder_gap_up = [2.0, 1.5, 1.2, 1.0, 0.8]
        if data.get("ladder_gap_dn") is not None:
            st.ladder_gap_dn = data.get("ladder_gap_dn")
        elif _old_dens is not None:
            st.ladder_gap_dn = [max(round(d * 1.0, 2), 0.3) for d in _old_dens]
        else:
            st.ladder_gap_dn = [1.0, 0.8, 0.7, 0.5, 0.4]
        # 兼容旧仓(无 ladder_enabled)：默认全启用
        st.ladder_enabled = data.get("ladder_enabled", [True, True, True, True, True])
        st.confirm_time = data.get("confirm_time", 30.0)
        st.debounce_loss_line = data.get("debounce_loss_line", -0.5)
        st.debounce_profit_line = data.get("debounce_profit_line", 0.2)
        st.atr_timeframe = data.get("atr_timeframe", "1H")
        st.atr_period = data.get("atr_period", 24)
        st.density_min = data.get("density_min", 0.3)
        st.cumulative_added = data.get("cumulative_added", 0.0)
        st.safety_flat_threshold = data.get("safety_flat_threshold", 5.0)
        st.auto_rebuild_blocked = data.get("auto_rebuild_blocked", False)
        st.flat_reason = data.get("flat_reason", "")
        st.flat_ts = data.get("flat_ts", 0.0)
        # 批次3：单边趋势参数化
        st.trend_tf = data.get("trend_tf", "4H")
        st.ema_fast = data.get("ema_fast", 10)
        st.ema_slow = data.get("ema_slow", 55)
        st.st_period = data.get("st_period", 14)
        st.st_mult = data.get("st_mult", 3.0)
        st.iceberg_sz = data.get("iceberg_sz", 2)
        st.pxVar = data.get("pxVar", 1.0)
        st.auto_adjust = data.get("auto_adjust", True)
        st.grid_auto_run = data.get("grid_auto_run", True)
        st.use_iceberg = data.get("use_iceberg", True)
        st.use_risk_control = data.get("use_risk_control", True)
        st.use_bleed_melt = data.get("use_bleed_melt", True)
        st.use_safety_flat = data.get("use_safety_flat", True)
        st.use_dynamic_params = data.get("use_dynamic_params", True)
        st.coin_amplitude_24h = data.get("coin_amplitude_24h", 0)
        st.amp_7d = data.get("amp_7d", 0)
        st.dr_24h = data.get("dr_24h", 0)
        st.dr_7d = data.get("dr_7d", 0)
        st.data_loop_interval = data.get("data_loop_interval", 2.0)
        st.oi_full_refresh_interval = data.get("oi_full_refresh_interval", 30.0)
        st.oi_history_size = data.get("oi_history_size", 30)
        st.oi_sample_count = data.get("oi_sample_count", 3)
        st.bleed_window_sec = data.get("bleed_window_sec", 5.0)
        st.equity_history_window_sec = data.get("equity_history_window_sec", 30.0)
        st.api_timeout = data.get("api_timeout", 10)
        st.health_stale_sec = data.get("health_stale_sec", 30.0)
        st.health_max_failures = data.get("health_max_failures", 3)
        st.equity_history = data.get("equity_history", [])
        st.adjust_history = data.get("adjust_history", [])
        st.logs = data.get("logs", [])
        st.adjust_records = data.get("adjust_records", [])
        st.adjust_seen_ord_ids = data.get("adjust_seen_ord_ids", [])
        st.build_ts = data.get("build_ts", 0.0)
    except Exception as e:
        print(f"load_state: {e}")
    return st
