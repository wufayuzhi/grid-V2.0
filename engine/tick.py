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
    """异步主循环：延迟后刷新 ticker/OI/持仓余额，触发决策"""
    loop = asyncio.get_event_loop()
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
            _refresh_ticker()
            _refresh_oi_current()
            _refresh_account()
            _trigger_decision()
            loop.run_in_executor(None, _refresh_oi_full)
        except Exception as e:
            logger.error(f"data_loop: {e}")


def _refresh_ticker():
    from data.ticker import refresh_ticker
    refresh_ticker()


def _refresh_oi_current():
    from data.ticker import refresh_oi_current
    refresh_oi_current()


def _refresh_oi_full():
    from data.ticker import refresh_oi
    refresh_oi()


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
    from formulas.safety import calc_safety_distance
    p = st.position
    st.safety_distance_pct = round(calc_safety_distance(
        p.long_contracts, p.short_contracts,
        p.long_liq_px, p.short_liq_px, p.mark_px), 2)

    # 高水位净浮盈 peak_upl 更新（累计回撤兜底用）：取 自上次全平/重建起的最高净浮盈
    _latest_upl = (p.long_unrealized_pnl or 0) + (p.short_unrealized_pnl or 0)
    if _latest_upl > st.peak_upl:
        st.peak_upl = _latest_upl

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
