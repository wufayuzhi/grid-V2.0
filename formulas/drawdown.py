"""formulas/drawdown.py — 累计回撤兜底（纯函数，无副作用）

统一优先级裁决链（2026-08-06 定稿）：
  累计回撤 = (自上次全平/重建以来的峰值upl − 最新upl) ÷ (账户权益 − 累计追加本金) × 100%
  累计回撤 > 累计回撤阈值 → 全平 + 禁止自动重建

口径（用户确认）：
  - 分子 = (峰值upl − 最新upl)，分母 = 账户权益 − 累计追加本金（BUG-B已修，不用峰值upl当分母）
  - 峰值upl = 自上次全平/重建以来的最高净浮盈（高水位）
"""
from __future__ import annotations


def calc_cumulative_drawdown_pct(peak_upl: float, latest_upl: float,
                                 total_equity: float,
                                 cumulative_added: float = 0.0) -> float:
    """累计回撤%。

    peak_upl: 高水位净浮盈（自上次全平/重建起最高）
    latest_upl: 当前净浮盈（长upl + 短upl）
    total_equity: 账户权益
    cumulative_added: 累计追加本金（分母剔除）
    """
    denom = max(float(total_equity or 0) - float(cumulative_added or 0), 1e-9)
    drawdown = (peak_upl - latest_upl) / denom * 100
    return max(drawdown, 0.0)  # 净浮盈高于峰值时不触发（未回撤）


def should_flat_by_drawdown(peak_upl: float, latest_upl: float,
                            total_equity: float, threshold_pct: float,
                            cumulative_added: float = 0.0) -> tuple[bool, float]:
    """累计回撤判定。返回 (是否触发, 当前回撤%)。"""
    if threshold_pct <= 0:
        return False, 0.0
    dd = calc_cumulative_drawdown_pct(peak_upl, latest_upl, total_equity, cumulative_added)
    return dd > threshold_pct, round(dd, 2)
