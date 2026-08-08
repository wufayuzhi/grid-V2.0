"""
formulas/grid.py — 网格公式（纯函数，无副作用）

新架构原则：间距 = 最近 N 根日线的平均真实波幅(TR) × 基础比例。
锚点使用交易所最新成交价（mark_px / last_px），不使用成本中轴。

已废弃（最终方案确认删除）：
  - DR 放大间距（f_dr = 1 + 3*DR）→ 已移除
  - 三区间 K 线缩放 → 已移除
"""
from __future__ import annotations


def true_range(high: float, low: float, prev_close: float) -> float:
    """单根K线真实波幅 TR = max(high-low, |high-prev_close|, |low-prev_close|)。"""
    if high <= 0 or low <= 0:
        return 0.0
    return max(high - low, abs(high - prev_close) if prev_close > 0 else 0,
               abs(low - prev_close) if prev_close > 0 else 0)


def avg_true_range(bars: list[dict]) -> float:
    """平均真实波幅 ATR = mean(TR)。bars 为按时间升序的K线 [{high,low,close}]。"""
    if not bars:
        return 0.0
    prev_close = 0.0
    trs = []
    for b in bars:
        h = float(b.get("high", 0) or 0)
        l = float(b.get("low", 0) or 0)
        c = float(b.get("close", 0) or 0)
        tr = true_range(h, l, prev_close)
        trs.append(tr)
        prev_close = c
    if not trs:
        return 0.0
    return sum(trs) / len(trs)


def grid_spacing_pct(atr: float, mark_px: float, base_ratio: float = 1.0) -> float:
    """网格间距% = ATR(绝对价) / 锚点价 × 基础比例。

    锚点是交易所最新成交价/标记价。间距只由真实波幅决定（定稿：不放大DR、不用24h振幅权重）。
    atr 必须是绝对价格（如 1500 USDT），不是百分比——传百分比是单位错位。
    """
    if mark_px <= 0 or atr <= 0:
        return 0.0
    return atr / mark_px * 100 * base_ratio


def grid_prices(mark_px: float, spacing_pct: float) -> tuple[float, float]:
    """挂单价：上方=平多开空，下方=平空开多。以成交价为锚对称挂单。"""
    if mark_px <= 0:
        return 0.0, 0.0
    upper = mark_px * (1 + spacing_pct / 100)
    lower = mark_px * (1 - spacing_pct / 100)
    return upper, lower


def anchor_price(last_px: float, mark_px: float) -> float:
    """锚点价：优先最新成交价(last_px)，无则用标记价(mark_px)。"""
    return last_px if last_px > 0 else mark_px
