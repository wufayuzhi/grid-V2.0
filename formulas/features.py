"""
formulas/features.py — 币种特征计算（纯函数，无副作用）

计算交易所需但交易所不直接提供的衍生特征：
  - 综合振幅（24h 与 7d 加权）
  - 方向性比率（DR）
"""
from __future__ import annotations


def amp_composite(amp_24h: float, amp_7d: float,
                  w24: float = 0.4, w7d: float = 0.6) -> float:
    """综合振幅% = 0.4×24h + 0.6×7d。缺数据时用另一项或默认。"""
    if amp_24h <= 0 and amp_7d <= 0:
        return 3.0
    if amp_24h <= 0:
        return amp_7d
    if amp_7d <= 0:
        return amp_24h
    return w24 * amp_24h + w7d * amp_7d


def dr_composite(dr_24h: float, dr_7d: float,
                 w24: float = 0.4, w7d: float = 0.6) -> float:
    """综合方向性比率 0=纯震荡 1=完全单边。"""
    return w24 * dr_24h + w7d * dr_7d


def refresh_coin_features(st) -> None:
    """刷新 state 上的综合特征（供前端展示 / 决策层使用）。"""
    st.amp_composite = amp_composite(st.coin_amplitude_24h, st.amp_7d)
    st.dr_composite = dr_composite(st.dr_24h, st.dr_7d)
