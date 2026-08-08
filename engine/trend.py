"""
engine/trend.py — 单边趋势检测（决策层）

新架构：ST(SuperTrend) + EMA 双确认判定方向（文档§单边趋势 定稿）。
只返回方向数据，不触发交易（决策由 adjust 做）。

参数全部从 state 读取（文档§单边趋势 2.1 参数化）：
  trend_tf   : 趋势时间框架（默认4H，下拉 1H/4H/6H/1D）
  ema_fast   : EMA快周期（默认10）
  ema_slow   : EMA慢周期（默认55，必须 > 快）
  st_period  : SuperTrend ATR周期（默认14）
  st_mult    : SuperTrend 乘数（默认3）
  不再硬编码。代码 vs 文档历史差异：旧代码硬编码 6H/ST10，已对齐文档 4H/ST14。
"""
from __future__ import annotations
import time
import logging

from data.ticker import get_client
from models import GridState
from state import get_state

logger = logging.getLogger(__name__)

_CACHE_TTL = 300  # 趋势检测缓存秒（文档无此参数，保留内部缓存控制）

# 缓存
_trend_cache: dict = {
    "direction": "", "st_direction": "", "ema10": 0.0, "ema55": 0.0,
    "last_check": 0.0, "inst_id": "",
}


def _get_state() -> GridState:
    """获取当前 state（读趋势参数）。"""
    return get_state()


def _trend_params(st) -> dict:
    """从 state 读趋势参数（无则用文档默认值）。"""
    return {
        "tf": getattr(st, "trend_tf", "4H") or "4H",
        "ema_fast": int(getattr(st, "ema_fast", 10) or 10),
        "ema_slow": int(getattr(st, "ema_slow", 55) or 55),
        "st_period": int(getattr(st, "st_period", 14) or 14),
        "st_mult": float(getattr(st, "st_mult", 3.0) or 3.0),
    }


def _fetch_candles(inst_id: str, bar: str) -> list[dict]:
    """拉取趋势K线（走 data 层公开客户端）。bar 由 trend_tf 决定。"""
    try:
        client = get_client()
        raw = client.get_candles(inst_id, bar=bar, limit=60)
        candles = []
        for c in raw or []:
            candles.append({
                "time": int(c[0]),
                "open": float(c[1]), "high": float(c[2]),
                "low": float(c[3]), "close": float(c[4]),
                "volume": float(c[5]),
            })
        return candles
    except Exception as e:
        logger.warning(f"_fetch_candles({bar}): {e}")
        return []


def _ema(values: list[float], period: int) -> list[float]:
    """标准 EMA: alpha=2/(N+1)，SMA 种子。"""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append((v - result[-1]) * k + result[-1])
    return result


def _atr(candles: list[dict], period: int) -> list[float]:
    """平均真实波幅 ATR。"""
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return []
    result = [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        result.append((trs[i] + result[-1] * (period - 1)) / period)
    return result


def _super_trend(candles: list[dict], atr_period: int, multiplier: float) -> list[dict]:
    """SuperTrend 计算。direction: Pine 标准 -1=向上(绿), 1=向下(红)。"""
    atr_list = _atr(candles, atr_period)
    if not atr_list:
        return []
    result = []
    hl2 = (candles[atr_period]["high"] + candles[atr_period]["low"]) / 2
    upper = hl2 + multiplier * atr_list[0]
    lower = hl2 - multiplier * atr_list[0]
    trend = 1  # 初始向下
    for i in range(atr_period, len(candles)):
        close = candles[i]["close"]
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2
        up_band = hl2 + multiplier * atr_list[i - atr_period]
        lo_band = hl2 - multiplier * atr_list[i - atr_period]
        if close > upper:
            upper = up_band
        else:
            upper = min(up_band, upper)
        if close < lower:
            lower = lo_band
        else:
            lower = max(lo_band, lower)
        if trend == 1 and close < lower:
            trend = -1
        elif trend == -1 and close > upper:
            trend = 1
        # Pine: -1=向上(绿), 1=向下(红)
        result.append({
            "time": candles[i]["time"],
            "value": lower if trend == -1 else upper,
            "direction": -trend if trend == -1 else -trend,
        })
    return result


def _detect(inst_id: str) -> dict:
    """执行完整趋势检测（参数从 state 读）。"""
    st = _get_state()
    p = _trend_params(st)
    ema_fast, ema_slow = p["ema_fast"], p["ema_slow"]
    st_period, st_mult = p["st_period"], p["st_mult"]
    candles = _fetch_candles(inst_id, p["tf"])
    if len(candles) < ema_slow:
        return {"direction": "neutral", "st_direction": "", "ema10": 0.0,
                "ema55": 0.0, "last_check": time.time(), "inst_id": inst_id}
    closes = [c["close"] for c in candles]
    ema10_list = _ema(closes, ema_fast)
    ema55_list = _ema(closes, ema_slow)
    ema10_val = ema10_list[-1]
    ema55_val = ema55_list[-1]
    ema_cross = ema10_val > ema55_val
    st_list = _super_trend(candles, st_period, st_mult)
    st_dir = st_list[-1]["direction"] if st_list else 0
    st_up = st_dir == -1  # Pine: -1=向上

    if st_up and ema_cross:
        direction, sd = "bull", "up"
    elif not st_up and not ema_cross:
        direction, sd = "bear", "down"
    else:
        direction, sd = "neutral", ("up" if st_up else "down")
    return {"direction": direction, "st_direction": sd,
            "ema10": round(ema10_val, 2), "ema55": round(ema55_val, 2),
            "last_check": time.time(), "inst_id": inst_id}


def _should_refresh(inst_id: str) -> bool:
    return (_trend_cache["inst_id"] != inst_id
            or time.time() - _trend_cache["last_check"] > _CACHE_TTL
            or _trend_cache["direction"] == "")


def get_trend_info() -> dict:
    """供前端展示趋势状态。"""
    st = _get_state()
    p = _trend_params(st)
    return {
        "direction": _trend_cache["direction"],
        "st_direction": _trend_cache["st_direction"],
        "ema10": _trend_cache["ema10"],
        "ema55": _trend_cache["ema55"],
        "ema_cross": _trend_cache["ema10"] > _trend_cache["ema55"],
        "last_check": _trend_cache["last_check"],
        "next_check": _trend_cache["last_check"] + _CACHE_TTL,
        "inst_id": _trend_cache["inst_id"],
        "params": {"tf": p["tf"], "st_period": p["st_period"], "st_mult": p["st_mult"],
                   "ema_fast": p["ema_fast"], "ema_slow": p["ema_slow"],
                   "cache_ttl_seconds": _CACHE_TTL},
    }


def ensure_trend(inst_id: str):
    """确保趋势已计算（首次或过期则刷新）。"""
    if inst_id and _should_refresh(inst_id):
        _trend_cache.update(_detect(inst_id))


def force_refresh(inst_id: str) -> dict:
    """手动强制刷新趋势（供前端按钮调用）。"""
    _trend_cache.update(_detect(inst_id))
    return get_trend_info()
