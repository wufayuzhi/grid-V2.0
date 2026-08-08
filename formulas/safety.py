"""
formulas/safety.py — 风控公式（纯函数，无副作用）

新架构原则：本模块只做计算，不读API、不碰state。
所有"交易所能提供"的数值（bePx/liqPx/margin/mgnRatio）由 data/ 层读取，
此处只用交易所已给的值计算，不自算强平价、不自算保证金。

已废弃（最终方案确认删除，不再实现）：
  - 三区间 zone 判定
  - 预警缓冲 6 因子乘法
  - 自算强平价 estimate_liqPx
  - DR 放大间距
"""
from __future__ import annotations


def calc_safety_distance(long_contracts: int, short_contracts: int,
                         long_liq_px: float, short_liq_px: float,
                         mark_px: float) -> float:
    """安全距离%。完全对冲→999%，有净敞口→净敞口方向的交易所强平价距离。"""
    if mark_px <= 0:
        return 999.0
    if long_contracts == short_contracts:
        return 999.0
    net = long_contracts - short_contracts
    liq = long_liq_px if net > 0 else short_liq_px
    if liq <= 0:
        return 999.0
    return abs(liq - mark_px) / mark_px * 100


def calc_imbalance_rate(long_contracts: int, short_contracts: int) -> float:
    """失衡率% = |多-空| / (多+空) × 100%。全为0→0。"""
    total = long_contracts + short_contracts
    if total <= 0:
        return 0.0
    return abs(long_contracts - short_contracts) / total * 100


def rebalance_contracts(long_contracts: int, short_contracts: int,
                        target_rate_pct: float) -> int:
    """回补张数：使失衡率回落到 target_rate_pct 需要平掉的净敞口张数。

    净敞口 = |多-空|。目标是让 净敞口/总仓位 ≈ target_rate。
    注意：回补是平掉盈利侧的张数，使两侧更均衡。
    """
    total = long_contracts + short_contracts
    if total <= 0:
        return 0
    net = abs(long_contracts - short_contracts)
    target_net = total * (target_rate_pct / 100)
    need = int(net - target_net)
    return max(need, 0)


def calc_safe_contracts(single_limit: float, safety_factor: float) -> int:
    """安全开仓张数 = 单边极限 × 安全系数（安全因子只用在安全开仓）。"""
    return max(int(single_limit * safety_factor), 0)
