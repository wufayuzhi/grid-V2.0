"""
data/ticker.py — 数据层：行情采集（标记价、振幅、OI等，只读）

新架构原则：只负责从 OKX 读取行情并写入 state / 维护 OI 缓存，
不含策略决策。决策在 engine/，计算在 formulas/。

参考旧 monitor/ticker.py + monitor/oi.py，改为基于 RawOkxRestClient。
"""
from __future__ import annotations
import time
import logging

from raw_rest_client import RawOkxRestClient
from state import get_state

logger = logging.getLogger(__name__)

# ─── 公开行情客户端（无需凭证）───
_pub: RawOkxRestClient | None = None

# 当前合约最新行情缓存（含 mark_px / 振幅等）
_ticker_cache: dict[str, dict] = {}

# OI 缓存与历史
_oi_cache: dict[str, dict] = {}
_oi_history: dict[str, list[float]] = {}
_oi_full_refresh: float = 0.0


def _safe_float(v, default: float = 0.0) -> float:
    """防崩溃转换：空串/None/非法 → default。模拟盘返回 '' 时不炸。"""
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def init_adapter() -> None:
    """初始化公开行情客户端（main.py startup 调用）。"""
    global _pub
    if _pub is not None and _pub.simulated == get_state().simulated:
        return
    st = get_state()
    _pub = RawOkxRestClient(timeout=st.api_timeout or 15, simulated=st.simulated)
    logger.info("data/ticker 公开行情客户端就绪")


def get_client() -> RawOkxRestClient:
    """获取公开行情客户端（未初始化或模式变化则重新初始化）。"""
    global _pub
    st = get_state()
    if _pub is None or _pub.simulated != st.simulated:
        init_adapter()
    assert _pub is not None
    return _pub


# ═══ 行情刷新（标记价 / 振幅 / 24h变化）═══

def refresh_ticker() -> dict | None:
    """拉取当前合约单条行情，写 mark_px / 振幅 / 变化率到 state。"""
    try:
        st = get_state()
        inst = st.inst_id
        if not inst:
            return None
        raw = get_client().get_ticker(inst)
        if not raw:
            return None
        return apply_ticker(inst, raw)
    except Exception as e:
        logger.warning(f"refresh_ticker: {e}")
        return None


def apply_ticker(inst_id: str, raw: dict) -> dict | None:
    """解析原始 ticker 并写入 state（可复用，便于注入测试）。"""
    try:
        last = _safe_float(raw.get("last"))
        if last <= 0:
            return None
        open24h = _safe_float(raw.get("open24h"))
        high24h = _safe_float(raw.get("high24h"))
        low24h = _safe_float(raw.get("low24h"))
        mark_px = _safe_float(raw.get("markPx")) or last
        # 指数价：/market/ticker 不含 idxPx，需单独拉 /market/index-tickers
        idx_px = _safe_float(raw.get("idxPx"))
        if idx_px <= 0:
            try:
                idx_raw = get_client().get_index_ticker(inst_id)
                idx_px = _safe_float(idx_raw.get("idxPx"))
            except Exception:
                idx_px = 0.0
        change_pct = ((last - open24h) / open24h * 100) if open24h > 0 else 0.0
        amp_pct = ((high24h - low24h) / open24h * 100) if open24h > 0 else 0.0
        vol_ccy = _safe_float(raw.get("volCcy24h"))

        entry = {
            "inst_id": inst_id,
            "last_px": last,
            "last": last,  # 兼容旧契约字段名
            "mark_px": mark_px,
            "idxPx": idx_px,
            "high24h": high24h,
            "low24h": low24h,
            "vol24h": _safe_float(raw.get("vol24h")),
            "volCcy24h": vol_ccy,
            "vol_ccy_24h": vol_ccy * last,
            "change_24h_pct": round(change_pct, 2),
            "amplitude_24h_pct": round(amp_pct, 2),
            "bid_px": _safe_float(raw.get("bidPx")),
            "ask_px": _safe_float(raw.get("askPx")),
            "markPx": mark_px,  # 兼容旧契约字段名
            "ts": time.time(),
            "src": "rest",
        }
        _ticker_cache[inst_id] = entry

        st = get_state()
        if inst_id == st.inst_id:
            st.position.mark_px = mark_px
            st.position.last_px = last
            st.coin_amplitude_24h = amp_pct
            st.dr_24h = abs(change_pct) / amp_pct if amp_pct > 0 else 0.0
        return entry
    except Exception as e:
        logger.warning(f"apply_ticker: {e}")
        return None


def get_ticker(inst_id: str) -> dict | None:
    """拉取指定合约行情（不入 state，返回解析结果）。"""
    try:
        raw = get_client().get_ticker(inst_id)
        if not raw:
            return None
        return apply_ticker(inst_id, raw)
    except Exception as e:
        logger.warning(f"get_ticker({inst_id}): {e}")
        return None


def get_mark_px() -> float:
    """获取当前合约标记价。"""
    st = get_state()
    if st.inst_id in _ticker_cache:
        return _ticker_cache[st.inst_id]["mark_px"]
    return st.position.mark_px


def get_last_px() -> float:
    """获取当前合约最新成交价（网格锚点主源）。"""
    st = get_state()
    if st.inst_id in _ticker_cache:
        return _ticker_cache[st.inst_id].get("last_px", 0.0)
    return st.position.last_px


def refresh_daily_atr(st=None) -> float:
    """拉K线算真实波幅ATR（绝对价），写入 st.atr_abs / st.atr_pct。

    ATR = mean(TR), TR=max(高-低,|高-前收|,|低-前收|)，取最近 N 根K线。
    时间框架/周期由 atr_timeframe / atr_period 参数控制（文档§1.2，默认 1H / N=24）。
    间距用绝对价 ATR（不是振幅百分比），这是网格间距的数据源。
    """
    st = st or get_state()
    if not st.inst_id:
        return 0.0
    try:
        tf = getattr(st, "atr_timeframe", "1H") or "1H"
        period = int(getattr(st, "atr_period", 24) or 24)
        raw = get_client().get_candles(st.inst_id, bar=tf, limit=period + 1)
        if not raw:
            return 0.0
        # OKX candles: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        bars = []
        for c in raw:
            h = _safe_float(c[2]); l = _safe_float(c[3]); cl = _safe_float(c[4])
            if h > 0 and l > 0:
                bars.append({"high": h, "low": l, "close": cl})
        if not bars:
            return 0.0
        from formulas.grid import avg_true_range
        atr = avg_true_range(bars)
        st.atr_abs = atr
        mark = st.position.mark_px
        st.atr_pct = atr / mark * 100 if mark > 0 else 0.0
        _maybe_notify_atr_change(st, prev_pct=getattr(st, "atr_pct_prev", 0.0), new_pct=st.atr_pct)
        st.atr_pct_prev = st.atr_pct
        return atr
    except Exception as e:
        logger.warning(f"refresh_daily_atr: {e}")
        return 0.0


def _maybe_notify_atr_change(st, prev_pct, new_pct) -> None:
    """ATR剧烈变化 → 企微推送（供人工决策是否调密度/间距）。

    阈值 = st.atr_notify_change_pct（默认 30%），表示 ATR% 相对上次变化超过此比例才推送。
    防刷屏：距上次推送不足 st.atr_notify_cooldown（默认 6h）则跳过。
    只推送不动仓、不改参数。
    """
    if prev_pct <= 0 or new_pct <= 0:
        return
    threshold = float(getattr(st, "atr_notify_change_pct", 30.0) or 30.0)
    # 变化比例：以旧值为基准的相对变化 |新-旧|/旧*100
    change_pct = abs(new_pct - prev_pct) / prev_pct * 100
    if change_pct < threshold:
        return
    cooldown = float(getattr(st, "atr_notify_cooldown", 6 * 3600.0) or 6 * 3600.0)
    last = getattr(st, "atr_last_push_ts", 0.0) or 0.0
    if last > 0 and (time.time() - last) < cooldown:
        return
    st.atr_last_push_ts = time.time()
    direction = "↑" if new_pct > prev_pct else "↓"
    try:
        from notify import notify
        notify("atr", "📊 ATR波动变化",
               [f"ATR% {prev_pct:.2f}% → {new_pct:.2f}% ({direction}{change_pct:.0f}%)",
                f"币种 {st.inst_id} | 密度 {getattr(st,'current_density','—')} | 间距 {getattr(st,'grid_spacing_pct','—')}%",
                "请人工判断是否需要调整网格密度/间距"])
    except Exception as e:
        logger.warning(f"notify_atr_change: {e}")


# ═══ OI（持仓量）═══

def refresh_oi() -> None:
    """全量 OI 刷新（按 oi_full_refresh_interval 节流），只刷缓存中的合约。"""
    global _oi_full_refresh
    st = get_state()
    now = time.time()
    if now - _oi_full_refresh < st.oi_full_refresh_interval:
        return
    _oi_full_refresh = now
    for inst_id in list(_ticker_cache.keys()):
        fetch_oi(inst_id)


def refresh_oi_current() -> None:
    """只刷新当前合约的 OI。"""
    st = get_state()
    if st.inst_id:
        fetch_oi(st.inst_id)


def fetch_oi(inst_id: str) -> float | None:
    """拉取单合约 OI 并记录历史，返回当前 OI 值（失败返回 None）。"""
    try:
        raw = get_client().get_open_interest(inst_id)
        if not raw:
            return None
        val = _safe_float(raw.get("oi"))
        if val <= 0:
            return None
        _oi_cache[inst_id] = {"oi": val, "oi_currency": raw.get("oiCcy", "")}
        st = get_state()
        hist = _oi_history.setdefault(inst_id, [])
        hist.append(val)
        if len(hist) > st.oi_history_size:
            _oi_history[inst_id] = hist[-st.oi_history_size:]
        return val
    except Exception as e:
        logger.warning(f"fetch_oi({inst_id}): {e}")
        return None


def get_oi_change(inst_id: str) -> float:
    """计算 OI 变化率（%）：最近 sample_count 平均 对 前 sample_count 平均。"""
    st = get_state()
    n = st.oi_sample_count
    hist = _oi_history.get(inst_id, [])
    if len(hist) < n * 2:
        return 0.0
    recent = sum(hist[-n:]) / n
    prev = sum(hist[-n * 2:-n]) / n
    if prev <= 0:
        return 0.0
    return (recent - prev) / prev * 100


def get_oi_cache() -> dict[str, dict]:
    """获取 OI 缓存（{inst_id: {"oi":..., "oi_currency":...}}）。"""
    return _oi_cache


def get_ticker_cache() -> list[dict]:
    """获取行情缓存（list of entries，兼容旧契约）。

    内部用 dict 存储（{inst_id: entry}），对外返回 list 供 routes 迭代。
    """
    return list(_ticker_cache.values())


# ═══ WS 推送更新层（模块A：OKX WebSocket 公共频道实时行情 → 统一缓存）═══
# 与 REST 走同一个 _ticker_cache，WS 主通道(毫秒级) + 低频REST 5s并线兜底(时间戳并线取最新)。
# 每个 entry 带 ts(更新时间) / src(ws|rest)，供 stale 校验与交易用价保护。

# 标记价/最新价 stale 阈值(秒)：超过视为行情卡住(WS断线未兜底)，调用方须降级保护
MARK_STALE_TS = 15.0


def _new_entry(inst_id: str) -> dict:
    """取或初始化某合约的行情 entry（WS 更新复用，避免覆盖 REST 已填字段）。"""
    e = _ticker_cache.get(inst_id)
    if e is None:
        e = {"inst_id": inst_id, "ts": 0.0, "src": "none"}
        _ticker_cache[inst_id] = e
    return e


def apply_ws_ticker(inst_id: str, d: dict) -> None:
    """OKX WS tickers 频道推送 → 更新最新价/24h高低/量/额/买卖盘口(并算涨跌/振幅)。"""
    try:
        last = _safe_float(d.get("last"))
        if last <= 0:
            return
        e = _new_entry(inst_id)
        open24h = _safe_float(d.get("open24h"))
        high24h = _safe_float(d.get("high24h"))
        low24h = _safe_float(d.get("low24h"))
        vol_ccy = _safe_float(d.get("volCcy24h"))
        e["last_px"] = last
        e["last"] = last
        e["high24h"] = high24h
        e["low24h"] = low24h
        e["vol24h"] = _safe_float(d.get("vol24h"))
        e["volCcy24h"] = vol_ccy
        e["vol_ccy_24h"] = vol_ccy * last
        e["bid_px"] = _safe_float(d.get("bidPx"))
        e["ask_px"] = _safe_float(d.get("askPx"))
        if open24h > 0:
            e["change_24h_pct"] = round((last - open24h) / open24h * 100, 2)
            e["amplitude_24h_pct"] = round((high24h - low24h) / open24h * 100, 2)
        e["ts"] = time.time()
        e["src"] = "ws"
        _sync_state(inst_id, e)
    except Exception as ex:
        logger.warning(f"apply_ws_ticker: {ex}")


def apply_ws_mark_px(inst_id: str, d: dict) -> None:
    """OKX WS mark-price 频道推送 → 更新标记价(交易用价主源，独立于最新价)。"""
    try:
        mark = _safe_float(d.get("markPx"))
        if mark <= 0:
            return
        e = _new_entry(inst_id)
        e["mark_px"] = mark
        e["markPx"] = mark
        e["ts"] = time.time()
        e["src"] = "ws"
        _sync_state(inst_id, e)
    except Exception as ex:
        logger.warning(f"apply_ws_mark_px: {ex}")


def apply_ws_idx_px(inst_id: str, d: dict) -> None:
    """OKX WS index-tickers 频道推送 → 更新指数价。"""
    try:
        idx = _safe_float(d.get("idxPx"))
        if idx <= 0:
            return
        e = _new_entry(inst_id)
        e["idxPx"] = idx
        e["ts"] = time.time()
        e["src"] = "ws"
        _sync_state(inst_id, e)
    except Exception as ex:
        logger.warning(f"apply_ws_idx_px: {ex}")


def apply_ws_oi(inst_id: str, d: dict) -> None:
    """OKX WS open-interest 频道推送 → 更新 OI 缓存与历史。"""
    try:
        val = _safe_float(d.get("oi"))
        if val <= 0:
            return
        _oi_cache[inst_id] = {"oi": val, "oi_currency": d.get("oiCcy", "")}
        hist = _oi_history.setdefault(inst_id, [])
        hist.append(val)
        st = get_state()
        if len(hist) > st.oi_history_size:
            _oi_history[inst_id] = hist[-st.oi_history_size:]
    except Exception as ex:
        logger.warning(f"apply_ws_oi: {ex}")


def _sync_state(inst_id: str, e: dict) -> None:
    """若更新的是当前合约，同步到 state(标记价/最新价/振幅/DR)。"""
    try:
        st = get_state()
        if inst_id != st.inst_id:
            return
        mark = e.get("mark_px") or e.get("markPx") or 0.0
        last = e.get("last_px") or 0.0
        if mark > 0:
            st.position.mark_px = mark
        if last > 0:
            st.position.last_px = last
        amp = e.get("amplitude_24h_pct", 0) or 0
        if amp:
            st.coin_amplitude_24h = amp
            chg = e.get("change_24h_pct", 0) or 0
            st.dr_24h = abs(chg) / amp if amp > 0 else 0.0
    except Exception as ex:
        logger.warning(f"_sync_state: {ex}")


# ─── K线缓存（WS candle 频道增量 + REST 首拉初始化）───

# K线缓存：{inst_id: {"tf": str, "bars": [[ts,o,h,l,c,vol,...]], "ts": float}}
_candle_cache: dict[str, dict] = {}


def init_candle_cache(inst_id: str, tf: str, limit: int = 300) -> None:
    """REST 首拉历史K线初始化缓存（启动/切币/切tf时调用），后续由 WS 增量更新。"""
    try:
        raw = get_client().get_candles(inst_id, bar=tf, limit=limit)
        if not raw:
            return
        # OKX get_candles 返回倒序(新在前) → 转正序(旧→新) 适配前端/增量追加
        _candle_cache[inst_id] = {"tf": tf, "bars": list(reversed(raw)), "ts": time.time()}
        logger.info(f"init_candle_cache({inst_id},{tf}) 首拉 {len(raw)} 根")
    except Exception as ex:
        logger.warning(f"init_candle_cache({inst_id},{tf}): {ex}")


def apply_ws_candle(inst_id: str, d: list, tf: str) -> None:
    """OKX WS candle 频道推送(单根K线, 同ts替换/新ts追加) → 维护滚动K线缓存。"""
    try:
        limit = int(getattr(get_state(), "candle_ws_limit", 300) or 300)
        cc = _candle_cache.get(inst_id)
        if cc is None or cc.get("tf") != tf:
            cc = {"tf": tf, "bars": [], "ts": 0.0}
            _candle_cache[inst_id] = cc
        bars = cc["bars"]
        new_ts = d[0]
        replaced = False
        for i, b in enumerate(bars):
            if b[0] == new_ts:
                bars[i] = d
                replaced = True
                break
        if not replaced:
            bars.append(d)
        if len(bars) > limit:
            cc["bars"] = bars[-limit:]
        cc["ts"] = time.time()
    except Exception as ex:
        logger.warning(f"apply_ws_candle: {ex}")


def get_candle_cache(inst_id: str, tf: str) -> list:
    """获取指定合约/时间框架的K线缓存(空=未初始化/切tf后未首拉)。"""
    cc = _candle_cache.get(inst_id)
    if cc and cc.get("tf") == tf:
        return cc["bars"]
    return []


# ─── stale 校验（交易用价保护，模块A 🔴）───


def get_entry(inst_id: str) -> dict | None:
    """取某合约行情 entry。"""
    return _ticker_cache.get(inst_id)


def market_is_stale(inst_id: str, max_age: float = MARK_STALE_TS) -> bool:
    """当前行情是否超过 max_age 秒未更新(WS+REST均断线/卡住)。"""
    e = _ticker_cache.get(inst_id)
    if e is None or not e.get("ts"):
        return True
    return (time.time() - e["ts"]) > max_age
