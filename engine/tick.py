"""engine/tick.py — 数据主循环（新架构 backend_v2）

职责：异步 data_loop，每 data_loop_interval 秒刷新
  1. ticker 行情   → data.ticker
  2. OI（持仓量）   → data.oi
  3. 持仓/余额      → data.exchange
  4. 触发决策        → engine.adjust(check_adjust) + engine.grid(check_grid_tick)

依赖方向：
  - 行情读走 data.ticker
  - 持仓读走 data.exchange
  - 公式算走 formulas/
  - 决策执行走 engine.adjust / engine.grid
"""
from __future__ import annotations
import asyncio
import logging
import time

from state import get_state
from diagnostic_logger import get_diag_logger

logger = logging.getLogger(__name__)

# 日线ATR刷新节流（日线K线变化慢，无需每2s拉一次）
_atr_last_refresh = 0.0
_ATR_REFRESH_INTERVAL = 60.0

# 账单权威重算节流（2026-08-17 新增，方案B：周期用账单覆盖引擎自算值，几分钟迟滞避开限流）
_bills_last_refresh = 0.0
_BILLS_REFRESH_INTERVAL = 600.0  # 每10分钟用账单权威值覆盖(低频→撞429概率减半); 成交dirty仍即时


def _clear_residual_stats(st) -> None:
    """清掉 state 里可能残留的网格/收益统计（切模式时可能带入的模拟盘数据）。

    只清统计快照，不动 total_equity（让正确 client 的 sync_equity 覆盖）和历史记录。
    清零后 grid_count=0 会触发 backfill_grid_stats 从对应模式订单历史权威重算。
    """
    st.grid_count = 0
    st.total_pnl = 0.0
    st.total_fee = 0.0
    st.imbalance_rate = 0.0
    logger.info("已清空残留网格/收益统计，等待从订单历史权威重算")


async def data_loop():
    """异步主循环：延迟后刷新 ticker/OI/持仓余额，触发决策

    修复🔴6：整块 tick 体(_loop_iteration)跑在 worker 线程(run_in_executor)，
    内部 API 阻塞调用(含 time.sleep(60) 限流降级)不再冻结 asyncio 事件循环，
    前端/API 路由照常响应。顺序语义保持不变（ticker→OI→账户→决策），
    单 worker 串行执行，state 不会被并发写。
    """
    loop = asyncio.get_event_loop()
    # 模块A：启动 WS 行情订阅后台任务（主通道，毫秒级实时；断线/切币/切tf自动处理）
    #   与下方 _refresh_ticker(REST) 并线：WS 主推 + 低频REST 兜底，缓存时间戳并线取最新。
    try:
        from engine.ws_market import start_ws_task
        start_ws_task(loop)
        logger.info("WS行情订阅后台任务已启动")
    except Exception as e:
        logger.warning(f"WS行情订阅启动失败(走REST兜底): {e}")
    # 引擎启动时自动回填网格统计(滚动/已实现/手续费，从交易所订单历史权威计算)
    try:
        from engine.grid import backfill_grid_stats
        st0 = get_state()
        # backfill 自身幂等：grid_count>0 时跳过重算，保留已有统计，避免每次启动
        # 被 OKX 限流(429)漏数覆盖为错误值。切模式/建仓已在 routes.py 清零后触发重算。
        backfill_grid_stats(st0)
    except Exception as e:
        logger.warning(f"启动回填网格统计失败: {e}")
    while True:
        st = get_state()
        await asyncio.sleep(st.data_loop_interval)
        try:
            # 整块跑在 worker 线程，不冻结事件循环（修🔴6）
            await loop.run_in_executor(None, _loop_iteration)
        except Exception as e:
            logger.error(f"data_loop: {e}")


def _loop_iteration() -> None:
    """单次完整 tick（同步，跑在 executor worker 线程）。

    顺序：ticker → OI当前 → 持仓/余额 → 决策 → OI全量(节流)。
    与旧 data_loop 内联体完全等价，仅由同步阻塞线程执行。
    """
    _refresh_ticker()
    _refresh_oi_current()
    _refresh_account()
    _trigger_decision()
    _refresh_oi_full()
    _refresh_bills_stats()  # 2026-08-17 方案B：周期用账单权威值覆盖引擎自算


def _refresh_ticker():
    from data.ticker import refresh_ticker
    refresh_ticker()


def _refresh_oi_current():
    from data.ticker import refresh_oi_current
    refresh_oi_current()


def _refresh_oi_full():
    from data.ticker import refresh_oi
    refresh_oi()


# 成交后即时重算标志：引擎成交/调平时置位，让 _refresh_bills_stats 跳过节流立即执行（实时层，零交易影响）
_bills_dirty = False
_bills_fail_count = 0  # 账单对账连续失败计数(429退避重试用): 成功清零


def mark_bills_dirty() -> None:
    """成交/调平发生后调用，标记下次账单重算跳过节流立即执行。只设标志，绝不影响交易逻辑。"""
    global _bills_dirty
    _bills_dirty = True


def _refresh_bills_stats() -> None:
    """2026-08-17 方案B：周期(每5分钟)用账单权威值覆盖引擎自算的 grid_count/total_fee/total_pnl。

    账单是唯一权威源(钱动了才有流水, 无引擎误判)。主引擎记账降级为完整性报警器。
    引擎成交时的 +1/+fees 保留为实时值, 此处周期覆盖为账单权威值(几分钟迟滞, 避开限流)。
    2026-08-19 模块B：成交后(引擎置 _bills_dirty)立即重算(跳过节流 + force 绕过缓存)，调平记录接近实时、绝无漏记。
    """
    global _bills_last_refresh, _bills_dirty, _bills_fail_count
    now = time.time()
    _was_dirty = _bills_dirty
    if not _was_dirty and now - _bills_last_refresh < _BILLS_REFRESH_INTERVAL:
        return
    _bills_dirty = False
    _bills_last_refresh = now
    try:
        from engine.grid import calc_bills_stats
        from state import save_state
        st = get_state()
        stats = calc_bills_stats(st, force=_was_dirty)
        if not stats.get("ok"):
            # 撞429/拉取失败: 指数退避快速重试(60s起,上限300s), 而非等完整周期; 成功则清零
            _bills_fail_count += 1
            _retry_after = min(60 * (2 ** (_bills_fail_count - 1)), 300)
            _bills_last_refresh = now - (_BILLS_REFRESH_INTERVAL - _retry_after)
            logger.warning(f"账单周期重算: 拉取失败({_bills_fail_count}连败), 保留引擎值; {_retry_after}s后重试")
            return
        _bills_fail_count = 0
        # 账单权威值覆盖(仅当账单非空且翻页完整)
        _intg = stats.get("integrity", {})
        if _intg and not _intg.get("ok", True):
            logger.warning(f"账单周期重算: 校验告警 {_intg.get('note')} — 仍按账单覆盖")
        st.grid_count = stats.get("grid_count", st.grid_count)
        st.total_fee = abs(stats.get("total_fee", 0.0))  # 正数: 前端 已实现+浮盈-手续费
        st.total_pnl = stats.get("total_pnl", st.total_pnl)
        st.adjust_records = (stats.get("records", []) or st.adjust_records or [])[-300:]
        # 撤销单低频识别：账单(bills)无撤销流水，撤销单从订单历史识别（2026-08-19 并入低频任务）
        try:
            from api.routes import _merge_canceled_records
            _merge_canceled_records(st)
        except Exception as _ce:
            logger.warning(f"撤销单识别失败: {_ce}")
        save_state()
        logger.info(f"账单周期重算: grid={st.grid_count} fee={st.total_fee:.2f} pnl={st.total_pnl:.2f}")
    except Exception as e:
        logger.warning(f"账单周期重算失败: {e}")


def _refresh_account():
    from data.exchange import refresh_exchange_data
    refresh_exchange_data()


def _trigger_decision():
    """触发网格决策：同步标记价 → 更新特征/ATR → 安全距离 → 调平/网格。"""
    global _atr_last_refresh
    st = get_state()

    # 标记价最新
    from data.ticker import get_mark_px, refresh_daily_atr
    mark_px = get_mark_px()
    if mark_px and mark_px > 0:
        st.position.mark_px = mark_px

    # 币种特征
    from formulas.features import refresh_coin_features
    refresh_coin_features(st)

    # 真实波幅 ATR（节流刷新，供网格间距用）
    now = time.time()
    if now - _atr_last_refresh >= _ATR_REFRESH_INTERVAL:
        refresh_daily_atr(st)
        _atr_last_refresh = now

    # 失衡率实时更新（不依赖运行状态，网格停止也刷新，避免前端残留死值）
    from formulas.safety import calc_imbalance_rate
    _p = st.position
    st.imbalance_rate = round(calc_imbalance_rate(
        _p.long_contracts, _p.short_contracts), 2)

    if not st.running:
        return

    # 安全距离实时更新（前端真实值，不再是恒999）
    # 🔴模块A a5：行情降级告警(WS+REST均断线>15s)——安全距离/风控基于陈旧mark_px，提醒用户。
    # 不阻断风控判断(避免漏判真实风险，且旧mark_px是保守方向)；仅告警。
    try:
        from data.ticker import market_is_stale
        if market_is_stale(getattr(st, "inst_id", "")):
            logger.warning(f"⚠️ 行情降级(数据陈旧>15s) inst={st.inst_id}，安全距离/风控基于陈旧标记价，请检查网络")
    except Exception:
        pass
    from formulas.safety import calc_safety_distance
    p = st.position
    st.safety_distance_pct = round(calc_safety_distance(
        p.long_contracts, p.short_contracts,
        p.long_liq_px, p.short_liq_px, p.mark_px), 2)

    # 权益记录（失血熔断滑动窗口用）
    record = getattr(st, "record_equity", None)
    if callable(record):
        record()

    # 调平决策（回补/止盈/熔断/爆表 → 执行）
    try:
        from engine.adjust import check_adjust
        check_adjust(st)
    except Exception as e:
        logger.error(f"check_adjust: {e}")

    # 网格挂单/触发/重挂（独立隔离，网格错误不影响调平）
    try:
        from engine.grid import check_grid_tick
        check_grid_tick(st)
    except Exception as e:
        logger.error(f"check_grid_tick: {e}")


def tick_grid():
    """同步执行一次完整决策（供外部按需调用）"""
    _refresh_ticker()
    _trigger_decision()
