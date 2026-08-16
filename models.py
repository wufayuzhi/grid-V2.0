"""
领域模型 —— 只定义数据字段，不藏公式。
所有计算在 formulas/ 层，状态在 state.py，此处仅数据结构。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from formulas.price_precision import px_round  # 价格按交易所 tickSz 对齐


@dataclass
class GridPosition:
    """持仓数据（全部来自交易所，不在这里算）"""
    inst_id: str = ""
    # 多头
    long_contracts: int = 0
    long_avg_px: float = 0.0
    long_unrealized_pnl: float = 0.0
    long_liq_px: float = 0.0      # 强平价（交易所）
    long_be_px: float = 0.0       # 盈亏平衡价（交易所）
    # 空头
    short_contracts: int = 0
    short_avg_px: float = 0.0
    short_unrealized_pnl: float = 0.0
    short_liq_px: float = 0.0     # 强平价（交易所）
    short_be_px: float = 0.0      # 盈亏平衡价（交易所）
    # 通用
    mark_px: float = 0.0          # 标记价
    last_px: float = 0.0          # 最新成交价（网格锚点主源）
    liqPx: float = 0.0            # 兼容字段（组合强平，交易所能给则给）
    position_margin: float = 0.0  # 持仓保证金（交易所）
    notional_usd: float = 0.0     # 名义价值（交易所）


@dataclass
class GridState:
    """网格状态 —— 只存数据 + 持久化，不藏公式"""
    # ── 运行状态 ──
    running: bool = False
    paused: bool = False
    inst_id: str = ""
    margin_mode: str = "usdt"     # "usdt" or "coin"
    simulated: bool = False
    ct_val: float = 0.0           # 合约面值（交易所）
    leverage: int = 20

    def _tick_sz_or(self, default: float = 0.0) -> float:
        """当前合约价格精度 tickSz（交易所下发），供前端对齐显示。查不到返回 default。"""
        try:
            from engine.sync import get_inst_tick_sz
            ts = get_inst_tick_sz(self.inst_id)
            return ts if ts and ts > 0 else default
        except Exception:
            return default

    # ── 账户 ──
    total_equity: float = 1000.0  # 总权益（交易所）
    capital: float = 1000.0       # 本金
    reserved_capital: float = 0.0
    total_pnl: float = 0.0
    total_fee: float = 0.0

    # ── 持仓 ──
    position: GridPosition = field(default_factory=GridPosition)

    # ── 网格状态 ──
    grid_upper_px: float = 0.0
    grid_lower_px: float = 0.0
    grid_anchor_px: float = 0.0   # 网格锚点（供前端计算器透明显示）
    grid_placed_ts: float = 0.0   # 最近一次网格挂单时间戳（24h不成交自动重挂用）
    grid_last_rehang_ts: float = 0.0  # 最近一次24h自动重挂时间（冷却防反复）
    grid_rehang_hours: float = 24.0   # 不成交多久自动重挂（小时）
    grid_rehang_cooldown: float = 3600.0  # 自动重挂最小间隔（秒）
    grid_last_trade_ts: float = 0.0   # 最近一次网格成交时间（12h不成交提醒计时起点）
    grid_12h_push_ts: float = 0.0     # 12h未成交提醒推送时间（防刷屏）
    grid_count: int = 0
    rebalance_cnt: int = 0   # 失衡回补次数（独立标记，与网格滚动次数分开）
    pending_iceberg: int = 0

    # ── 建仓 ──
    initial_contracts: int = 0
    single_limit: int = 0
    build_ts: float = 0.0

    # ── 人工参数（前端可调）──
    # 止盈线
    tp_base_pct: float = 1.0       # 止盈线基础%（24h）
    tp_window_hours: float = 24.0  # 止盈时间档（每档递增）
    # 失衡率
    imbalance_threshold_pct: float = 80.0   # 回补触发失衡率（0-100，默认80）
    rebalance_target_pct: float = 40.0      # 回补目标失衡率（0-100，默认40，须<触发）
    rebalance_batches: int = 2              # 回补批数（滑块2~3，默认2）
    rebalance_batch_gap_min: float = 20.0   # 批间间隔（分钟，滑块5/15/30，默认20）
    rebalance_limit_timeout_min: float = 10.0  # 限价超时未成交转市价（分钟，默认10）
    rebalance_market_after_timeout: bool = True  # 勾选=限价超时转市价；不勾选=只挂限价不转市价
    # 风控
    safety_factor: float = 0.7
    bleed_threshold_pct: float = 3.0
    # 网格
    target_spacing_pct: float = 0.60
    adj_ratio: float = 0.06
    price_offset_pct: float = 0.2
    adjust_split_ratio: float = 0.5
    # 冰山
    iceberg_sz: int = 2
    pxVar: float = 1.0
    # 开关
    auto_adjust: bool = True
    grid_auto_run: bool = True   # 网格自动运行（挂单）总开关，与失衡率解耦（2026-08-14）
    use_iceberg: bool = True
    use_risk_control: bool = True
    use_bleed_melt: bool = True
    use_safety_flat: bool = True                # 安全距离全平保险开关(2026-08-15新增,默认开)
    use_dynamic_params: bool = True
    # 失衡回补(B)开关：默认关=以A(挂单调价格/单边防堆仓)为主，失衡靠盈亏平衡点抬升渐进化解；
    # 开启时才在失衡>阈值时市价减重仓侧(主动砍仓)，默认停用(用户2026-08定案)
    use_rebalance: bool = False

    # ── 网格密度 / 失衡防堆仓（统一设计文档 2026-08-06 定稿，批次1）──
    base_density: float = 2.0          # 基础网格密度（进攻模式 base）
    defense_density: float = 2.0       # 防守网格密度（防守模式 base）
    mode: str = "attack"               # attack进攻 / defense防守（人工切）
    one_way_threshold: float = 60.0    # 单向成交阈值(%)
    loss_ratio_tight1: float = 5.0     # 贴价阈值1(%): 净浮亏/网格可用≤此值=保本夹逼
    loss_ratio_tight2: float = 20.0    # 贴价阈值2(%): 净浮亏/网格可用≥此值=贴到最近急平
    ladder_rates: list = field(default_factory=lambda: [40, 50, 60, 70, 80])   # 档位失衡率
    ladder_gap_up: list = field(default_factory=lambda: [2.0, 1.5, 1.2, 1.0, 0.8])  # 档位重仓侧手填价差%(挂远少成交)
    ladder_gap_dn: list = field(default_factory=lambda: [1.0, 0.8, 0.7, 0.5, 0.4])  # 档位轻仓侧手填价差%(挂近加速成交)
    ladder_enabled: list = field(default_factory=lambda: [True, True, True, True, True])  # 档位是否启用(勾选)
    # 收网状态机（2026-08-16 批次C2）：进网由档位表逐层定；退网阈值只管完全退出最后一下；冷静期防反转
    ladder_exit_pct: float = 15.0        # 退网阈值(%)：失衡<此值彻底退出收网（完全退出的线，只管最后一下）→ 进冷静期
    ladder_cooldown_min: float = 30.0    # 冷静期(分钟)：完全退出后按正常网格挂单，防趋势反转反复进出
    # bePx 防抖/紧急迟滞
    confirm_time: float = 30.0         # 确认时间 T(秒)
    debounce_loss_line: float = -0.5   # 防抖·亏损线(%)
    debounce_profit_line: float = 0.2  # 防抖·盈利线(%)
    # ATR 参数
    atr_timeframe: str = "1H"          # ATR 时间框架（默认1H）
    atr_period: int = 24               # ATR 周期 N
    density_min: float = 0.3           # 网格密度下限（滑块0.3~1.0；下限=max(设定, 2×费率÷ATR%)）
    # 统一口径
    cumulative_added: float = 0.0      # 累计追加本金（失血/累计回撤/防抖线分母剔除）

    # ── 全平防线（统一优先级裁决链）──
    safety_flat_threshold: float = 5.0          # 安全距离全平阈值%(滑块3~10)
    auto_rebuild_blocked: bool = False          # 熔断类全平后禁止自动重建(人工grid/start重置)
    flat_reason: str = ""                       # 最近一次全平原因(止盈/失血/累计回撤/安全距离/手动)
    flat_ts: float = 0.0                        # 最近一次全平时间戳

    # ── 批次3：单边趋势跟踪参数化（文档§单边趋势定稿，替代 trend.py 硬编码）──
    trend_tf: str = "4H"          # 趋势时间框架(文档:4H, 下拉1H/4H/6H/1D)
    ema_fast: int = 10            # EMA快周期(文档:10)
    ema_slow: int = 55            # EMA慢周期(文档:55)
    st_period: int = 14           # SuperTrend ATR周期(文档:14, 代码现在是10错)
    st_mult: float = 3.0          # SuperTrend 乘数(文档:3)


    # ── 币种特征 ──
    coin_amplitude_24h: float = 0.0
    amp_7d: float = 0.0
    dr_24h: float = 0.0
    dr_7d: float = 0.0
    atr_abs: float = 0.0          # 真实波幅ATR（绝对价，日线TR均值，网格间距用）
    atr_pct: float = 0.0          # ATR相对当前价百分比
    atr_pct_prev: float = 0.0     # 上一次ATR%（ATR剧烈变化推送用）
    atr_last_push_ts: float = 0.0  # 上次ATR变化推送时间（防刷屏）
    atr_notify_change_pct: float = 30.0  # ATR变化推送阈值(%)
    atr_notify_cooldown: float = 21600.0  # ATR推送最小间隔(秒, 默认6h)

    # ── 网格挂单追踪（事件驱动成交检测）──
    grid_upper_ord_ids: list = field(default_factory=list)   # 上端组(平多+开空) ordId
    grid_lower_ord_ids: list = field(default_factory=list)   # 下端组(平空+开多) ordId
    last_rebalance_ts: float = 0.0        # 上次回补时间戳（冷却防重复下单）

    # ── 时间 ──
    data_loop_interval: float = 2.0
    oi_full_refresh_interval: float = 30.0
    oi_history_size: int = 30
    oi_sample_count: int = 3
    bleed_window_sec: float = 5.0
    equity_history_window_sec: float = 30.0

    # ── API ──
    api_timeout: int = 10
    health_stale_sec: float = 30.0
    health_max_failures: int = 3

    # ── 记录 ──
    equity_history: list = field(default_factory=list)
    adjust_history: list = field(default_factory=list)
    logs: list = field(default_factory=list)
    adjust_records: list = field(default_factory=list)
    adjust_seen_ord_ids: list = field(default_factory=list)

    # ── 衍生展示值（由 formulas 计算写入，非交易所直接返回）──
    imbalance_rate: float = 0.0      # 失衡率%（公式计算）
    amp_composite: float = 0.0       # 综合振幅%（公式计算）
    dr_composite: float = 0.0        # 综合方向性比率（公式计算）
    safety_distance_pct: float = 999.0  # 安全距离%（公式计算）

    # ── 计算详情（供前端展示）──
    calc_details: dict = field(default_factory=dict)

    # ═══ 契约方法（前端依赖的展示结构）═══

    @property
    def grid_available(self) -> float:
        """可用于网格的资金 = 总权益 - 预留。"""
        if self.total_equity is None:
            return 0.0
        return self.total_equity - self.reserved_capital

    def add_log(self, msg: str, level: str = "INFO", cat: str = "STATE", data: dict | None = None):
        import time
        from diagnostic_logger import get_diag_logger
        get_diag_logger().log(level, cat, msg, data)
        self.logs.append({"ts": time.strftime("%H:%M:%S"),
                          "msg": msg, "level": level, "cat": cat})
        if len(self.logs) > 500:
            self.logs = self.logs[-300:]

    def record_equity(self):
        import time
        self.equity_history.append((time.time(), self.total_equity))
        now = time.time()
        self.equity_history[:] = [
            (t, e) for t, e in self.equity_history
            if now - t <= self.equity_history_window_sec]

    def to_dict(self) -> dict:
        """前端契约：返回完整展示结构（路径/字段名与旧版一致）。"""
        pos = self.position
        return {
            "running": self.running,
            "inst_id": self.inst_id,
            "margin_mode": self.margin_mode,
            "simulated": self.simulated,
            "ct_val": self.ct_val,
            "tick_sz": self._tick_sz_or(0.0),
            "mark_px": px_round(self.inst_id, pos.mark_px),
            "last_px": px_round(self.inst_id, pos.last_px),
            "liqPx": px_round(self.inst_id, pos.liqPx),
            "safety_distance_pct": round(self.safety_distance_pct, 2),
            "total_equity": round(self.total_equity, 2) if self.total_equity is not None else 0.0,
            "reserved_capital": round(self.reserved_capital, 2),
            "grid_available": round(self.grid_available, 2),
            "total_pnl": round(self.total_pnl, 2),
            "total_fee": round(getattr(self, "total_fee", 0), 2),
            "grid_count": self.grid_count,
            "rebalance_cnt": getattr(self, "rebalance_cnt", 0),
            "grid_upper_px": px_round(self.inst_id, self.grid_upper_px),
            "grid_lower_px": px_round(self.inst_id, self.grid_lower_px),
            "pending_iceberg": self.pending_iceberg,
            "net_exposure": pos.long_contracts - pos.short_contracts,
            "total_contracts": pos.long_contracts + pos.short_contracts,
            "imbalance_rate": round(self.imbalance_rate, 2),
            "dominant_side": "long" if pos.long_contracts > pos.short_contracts
            else ("short" if pos.short_contracts > pos.long_contracts else "none"),
            "position_margin": round(pos.position_margin, 2),
            "long": {
                "contracts": pos.long_contracts,
                "avg_px": px_round(self.inst_id, pos.long_avg_px),
                "unrealized_pnl": round(pos.long_unrealized_pnl, 2),
                "liq_px": px_round(self.inst_id, pos.long_liq_px),
                "be_px": px_round(self.inst_id, pos.long_be_px),
            },
            "short": {
                "contracts": pos.short_contracts,
                "avg_px": px_round(self.inst_id, pos.short_avg_px),
                "unrealized_pnl": round(pos.short_unrealized_pnl, 2),
                "liq_px": px_round(self.inst_id, pos.short_liq_px),
                "be_px": px_round(self.inst_id, pos.short_be_px),
            },
            "params": {
                "capital": round(self.grid_available, 2),
                "reserved": round(self.reserved_capital, 2),
                "grid_available": round(self.grid_available, 2),
                "leverage": self.leverage,
                "imbalance_threshold": self.imbalance_threshold_pct,
                "imbalance_threshold_pct": self.imbalance_threshold_pct,  # 前端滑块 key 别名
                "initial_contracts": self.initial_contracts,
                "paused": self.paused,
                "single_limit": self.single_limit,
                "iceberg_sz": self.iceberg_sz,
                "pxVar": self.pxVar,
                "auto_adjust": self.auto_adjust,
                "use_iceberg": self.use_iceberg,
                "use_risk_control": self.use_risk_control,
                "use_bleed_melt": self.use_bleed_melt,
                "use_rebalance": self.use_rebalance,
                "safety_factor": self.safety_factor,
                "bleed_threshold_pct": self.bleed_threshold_pct,
                "tp_base_pct": self.tp_base_pct,
                "tp_window_hours": self.tp_window_hours,
                "rebalance_target_pct": self.rebalance_target_pct,
                "rebalance_batches": self.rebalance_batches,
                "rebalance_batch_gap_min": self.rebalance_batch_gap_min,
                "rebalance_limit_timeout_min": self.rebalance_limit_timeout_min,
                "rebalance_market_after_timeout": self.rebalance_market_after_timeout,
                "price_offset_pct": self.price_offset_pct,
                "adjust_split_ratio": self.adjust_split_ratio,
                "data_loop_interval": self.data_loop_interval,
                "oi_full_refresh_interval": self.oi_full_refresh_interval,
                "oi_history_size": self.oi_history_size,
                "oi_sample_count": self.oi_sample_count,
                "bleed_window_sec": self.bleed_window_sec,
                "equity_history_window_sec": self.equity_history_window_sec,
                "api_timeout": self.api_timeout,
                "health_stale_sec": self.health_stale_sec,
                "health_max_failures": self.health_max_failures,
                "coin_amplitude_24h": round(self.coin_amplitude_24h, 2),
                "dr_24h": round(self.dr_24h, 3),
                "dr_7d": round(self.dr_7d, 3),
                "atr_abs": round(self.atr_abs, 2),
                "atr_pct": round(self.atr_pct, 3),
                "use_dynamic_params": self.use_dynamic_params,
                "target_spacing_pct": self.target_spacing_pct,
                "adj_ratio": self.adj_ratio,
                "base_density": self.base_density,
                "density_min": self.density_min,
                "defense_density": self.defense_density,
                "mode": self.mode,
                "one_way_threshold": self.one_way_threshold,
                "loss_ratio_tight1": self.loss_ratio_tight1,
                "loss_ratio_tight2": self.loss_ratio_tight2,
                "ladder_rates": list(self.ladder_rates),
                "ladder_gap_up": list(self.ladder_gap_up),
                "ladder_gap_dn": list(self.ladder_gap_dn),
                "ladder_enabled": list(self.ladder_enabled),
                "ladder_exit_pct": self.ladder_exit_pct,
                "ladder_cooldown_min": self.ladder_cooldown_min,
                "confirm_time": self.confirm_time,
                "debounce_loss_line": self.debounce_loss_line,
                "debounce_profit_line": self.debounce_profit_line,
                "atr_timeframe": self.atr_timeframe,
                "atr_period": self.atr_period,
                # 单边趋势组（前端滑块回填需要，set_params 可存但 to_dict 曾漏发）
                "trend_tf": getattr(self, "trend_tf", None),
                "ema_fast": getattr(self, "ema_fast", None),
                "ema_slow": getattr(self, "ema_slow", None),
                "st_period": getattr(self, "st_period", None),
                "st_mult": getattr(self, "st_mult", None),
                "oi_n": None,
                "current_density": getattr(self, "current_density", None),
                "grid_spacing_pct": round(float(getattr(self, "grid_spacing_pct", 0.0) or 0.0), 3),
                "grid_anchor_px": round(float(getattr(self, "grid_anchor_px", 0.0) or 0.0), 8),
                "emergency_state": getattr(self, "emergency_state", None),
                "heavy_side": getattr(self, "heavy_side", None),
                "heavy_state": getattr(self, "heavy_state", None),
                "safety_flat_threshold": self.safety_flat_threshold,
                "auto_rebuild_blocked": self.auto_rebuild_blocked,
                "flat_reason": self.flat_reason,
                "flat_ts": self.flat_ts,
                "grid_upper_px": px_round(self.inst_id, self.grid_upper_px),
                "grid_lower_px": px_round(self.inst_id, self.grid_lower_px),
            },
            "adjust_history": self.adjust_history[-20:],
            "calc_details": self.calc_details,
        }
