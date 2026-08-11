"""
formulas/price_precision.py — 价格精度工具（无模块级依赖，避免循环 import）

价格显示/round 一律按交易所 tickSz 对齐（交易所是规则制定方）。
  - tick_sz_decimals(tickSz) : 由 tickSz 推导小数位（0.000001→6, 0.1→1, 1→0）
  - px_round(inst_id, px)    : 按交易所 tickSz round 价格

为避免 models↔sync↔state 循环 import，此处不在模块级 import engine.sync，
改为在函数内惰性 import get_inst_tick_sz。
"""
from __future__ import annotations


def tick_sz_decimals(tick_sz) -> int:
    """由 tickSz 推导价格小数位：0.000001→6, 0.1→1, 1→0, 0.01→2。tickSz<=0 返回 4(兜底)。"""
    try:
        if tick_sz is None or tick_sz <= 0:
            return 4
        s = f"{tick_sz:.10f}".rstrip("0")
        if "." in s:
            return len(s.split(".")[1])
        return 0
    except Exception:
        return 4


def _tick_sz(inst_id: str) -> float:
    """取合约 tickSz，惰性 import 避免循环依赖。"""
    try:
        from engine.sync import get_inst_tick_sz
        return get_inst_tick_sz(inst_id)
    except Exception:
        return 0.0


def px_round(inst_id: str, px: float, fallback_decimals: int = 4) -> float:
    """按交易所 tickSz 对齐 round 价格。查不到 tickSz 时用 fallback_decimals。"""
    try:
        ts = _tick_sz(inst_id)
        dec = tick_sz_decimals(ts)
        return round(px, dec)
    except Exception:
        return round(px, fallback_decimals)
