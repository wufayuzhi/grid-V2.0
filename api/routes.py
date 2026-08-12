"""
api/routes.py — 所有 FastAPI 路由（新架构 backend_v2）

复刻旧版 grid-w2 后端的全部 API 端点契约（路径 + 参数 + 返回结构原样）。
内部逻辑改为调用新架构 data 层 / engine 层 / formulas 层。

端点薄壳：只转发，不藏策略逻辑。
  - state          → state.get_state() / state.to_dict()（契约）
  - 行情缓存       → data.ticker（get_ticker_cache / get_mark_px / get_oi_cache / get_oi_change）
  - 持仓/余额      → data.exchange（sync_positions / sync_equity）
  - 认证/可用合约  → engine.sync（is_auth_ready / get_auth_client / get_available_inst_ids）
  - 交易操作       → engine.build / flat / hedge / grid / adjust / sync
                     （Phase4 未实现时 try/except ImportError 返回占位，保证契约存在、前端不崩）

静态文件：GET / → FileResponse("static/v3.html")，路径相对 backend_v2/。
"""
from __future__ import annotations

import os
import json
import time
import math
import asyncio
import logging
import datetime
import importlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

import wecom_config  # 前端可配置的企微通知配置

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  防御性导入：data/engine 各层可能尚在并行搭建，逐个保护
# ══════════════════════════════════════════════════════════════════

# ── 状态（契约已实现）──
try:
    from state import get_state, save_state
except ImportError:  # pragma: no cover
    get_state = None
    save_state = None

# ── 诊断日志 ──
try:
    from diagnostic_logger import get_diag_logger
except ImportError:  # pragma: no cover
    get_diag_logger = None

# ── 价格精度（按交易所 tickSz 对齐）──
try:
    from formulas.price_precision import px_round as _px_round, tick_sz_decimals as _tick_sz_decimals
except ImportError:
    _px_round = None
    _tick_sz_decimals = None


def _safe_px_round(inst_id, px, fallback_decimals=4):
    """px_round 兜底封装：import 失败时退化为固定 round。"""
    if _px_round is not None:
        try:
            return _px_round(inst_id, px, fallback_decimals)
        except Exception:
            pass
    return round(px, fallback_decimals)


px_round = _safe_px_round  # 供本模块内使用（永不 None）
tick_sz_decimals = (_tick_sz_decimals or (lambda ts: 4))


# ── data 层：行情 ──
try:
    from data.ticker import (
        get_ticker_cache, get_mark_px, get_oi_cache, get_oi_change,
        get_ticker as _force_ticker_refresh,
        get_client as _get_ticker_client,
    )
except ImportError:
    get_ticker_cache = get_mark_px = get_oi_cache = get_oi_change = None
    _force_ticker_refresh = None
    _get_ticker_client = None

# ── data 层：持仓/余额 ──
try:
    from data.exchange import sync_positions, sync_equity
except ImportError:
    sync_positions = sync_equity = None

# ── engine 层：认证/可用合约 ──
try:
    from engine.sync import (
        load_apikey, save_apikey, build_auth_client, refresh_available_instruments,
        is_auth_ready, get_auth_client, get_available_inst_ids, get_inst_tick_sz,
    )
except ImportError:
    load_apikey = save_apikey = build_auth_client = refresh_available_instruments = None
    is_auth_ready = get_auth_client = get_available_inst_ids = None
    get_inst_tick_sz = None


def _safe_tick_sz(inst_id: str) -> float:
    """get_inst_tick_sz 兜底封装：import 失败返回 0。"""
    if get_inst_tick_sz is not None:
        try:
            return get_inst_tick_sz(inst_id)
        except Exception:
            pass
    return 0.0

# ── 公开行情客户端（杠杆档位等用）──
try:
    from raw_rest_client import RawOkxRestClient, get_api_stats
except ImportError:  # pragma: no cover
    RawOkxRestClient = None
    get_api_stats = None

# ══════════════════════════════════════════════════════════════════
#  杠杆列表 & 本地缓存（旧版来自 monitor.coin，此处收敛到 routes）
# ══════════════════════════════════════════════════════════════════
AVAILABLE_LEVERS = [1, 2, 3, 5, 10, 15, 20, 25, 33, 50, 75, 100, 125]

# 杠杆选项缓存（旧版 get_lev_opt_cache，此处用模块级 dict）
_lev_opt_cache: dict = {}
_lev_opt_cache_ts: dict = {}
_LEV_OPT_CACHE_TTL = 30.0

# 旧版 monitor.coin._change_7d_cache / _ct_val_cache —— 新架构暂缺，用本地空缓存
_change_7d_cache: dict = {}
_ct_val_cache: dict = {}


# ══════════════════════════════════════════════════════════════════
#  安全辅助函数（新 GridState 缺省字段的防御性兜底）
# ══════════════════════════════════════════════════════════════════

def _to_f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt_ms(ms):
    """毫秒时间戳 → 'MM-DD HH:MM:SS'"""
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S")
    except Exception:
        return ""


def _diag():
    if get_diag_logger is not None:
        try:
            return get_diag_logger()
        except Exception:
            return None
    return None


def _add_log(st, msg, level="INFO", cat="STATE", data=None):
    """复刻旧版 GridState.add_log：写诊断日志 + 内存日志环。"""
    d = _diag()
    if d is not None:
        try:
            d.log(level, cat, msg, data)
        except Exception:
            pass
    try:
        t = time.strftime("%H:%M:%S")
        logs = getattr(st, "logs", None)
        if logs is None:
            logs = []
            setattr(st, "logs", logs)
        logs.append({"ts": t, "msg": msg, "level": level, "cat": cat})
        if len(logs) > 500:
            logs[:] = logs[-300:]
    except Exception:
        pass


def _grid_available(st):
    """网格可用资金 = 总权益 - 预留（复刻旧版 GridState.grid_available）。"""
    try:
        te = getattr(st, "total_equity", None)
        if te is None:
            return 0.0
        return float(te) - float(getattr(st, "reserved_capital", 0.0))
    except Exception:
        return 0.0


def _state_dict(st):
    """返回 state 契约字典：优先 st.to_dict()（契约已实现），缺失时兜底组装。"""
    if hasattr(st, "to_dict"):
        try:
            return st.to_dict()
        except Exception:
            pass
    # ── 兜底：按字段名组装（契约字段名保持一致）──
    d = {}
    for k in (
        "running", "inst_id", "margin_mode", "simulated", "ct_val", "leverage",
        "total_equity", "reserved_capital", "total_pnl", "total_fee", "grid_count",
        "grid_upper_px", "grid_lower_px", "pending_iceberg", "initial_contracts",
        "single_limit", "build_ts", "paused", "target_spacing_pct", "adj_ratio",
        "price_offset_pct", "adjust_split_ratio", "iceberg_sz", "pxVar",
        "oi_spike_pct", "oi_drop_pct", "oi_lock_base", "auto_adjust", "use_iceberg",
        "use_risk_control", "use_bleed_melt", "use_dynamic_params",
        "coin_amplitude_24h", "amp_7d", "dr_24h", "dr_7d", "data_loop_interval",
        "oi_full_refresh_interval", "oi_history_size", "oi_sample_count",
        "bleed_window_sec", "equity_history_window_sec", "api_timeout",
        "health_stale_sec", "health_max_failures", "tp_base_pct", "tp_window_hours",
        "imbalance_threshold_pct", "imbalance_blowup_pct", "rebalance_target_pct",
        "safety_factor", "shrink_pct", "bleed_threshold_pct",
    ):
        d[k] = getattr(st, k, None)
    pos = getattr(st, "position", None)
    if pos is not None:
        _inst = getattr(st, "inst_id", "")
        d["mark_px"] = px_round(_inst, getattr(pos, "mark_px", 0.0))
        d["long"] = {
            "contracts": getattr(pos, "long_contracts", 0),
            "avg_px": px_round(_inst, getattr(pos, "long_avg_px", 0.0)),
            "unrealized_pnl": round(getattr(pos, "long_unrealized_pnl", 0.0), 2),
            "liq_px": px_round(_inst, getattr(pos, "long_liq_px", 0.0)),
        }
        d["short"] = {
            "contracts": getattr(pos, "short_contracts", 0),
            "avg_px": px_round(_inst, getattr(pos, "short_avg_px", 0.0)),
            "unrealized_pnl": round(getattr(pos, "short_unrealized_pnl", 0.0), 2),
            "liq_px": px_round(_inst, getattr(pos, "short_liq_px", 0.0)),
        }
    d["grid_available"] = round(_grid_available(st), 2)
    d["params"] = {
        "capital": round(_grid_available(st), 2),
        "reserved": round(getattr(st, "reserved_capital", 0.0), 2),
        "grid_available": round(_grid_available(st), 2),
        "leverage": getattr(st, "leverage", 20),
        "imbalance_threshold": getattr(st, "imbalance_threshold_pct", 20.0),
        "initial_contracts": getattr(st, "initial_contracts", 0),
        "paused": getattr(st, "paused", False),
        "single_limit": getattr(st, "single_limit", 0),
        "iceberg_sz": getattr(st, "iceberg_sz", 2),
        "pxVar": getattr(st, "pxVar", 1.0),
        "auto_adjust": getattr(st, "auto_adjust", True),
        "use_iceberg": getattr(st, "use_iceberg", True),
        "use_risk_control": getattr(st, "use_risk_control", True),
        "use_bleed_melt": getattr(st, "use_bleed_melt", True),
        "safety_factor": getattr(st, "safety_factor", 0.7),
        "shrink_pct": getattr(st, "shrink_pct", 10.0),
        "oi_spike_pct": getattr(st, "oi_spike_pct", 15.0),
        "oi_drop_pct": getattr(st, "oi_drop_pct", -5.0),
        "oi_lock_base": getattr(st, "oi_lock_base", 5),
        "bleed_threshold_pct": getattr(st, "bleed_threshold_pct", 3.0),
        "price_offset_pct": getattr(st, "price_offset_pct", 0.2),
        "adjust_split_ratio": getattr(st, "adjust_split_ratio", 0.5),
        "data_loop_interval": getattr(st, "data_loop_interval", 2.0),
        "oi_full_refresh_interval": getattr(st, "oi_full_refresh_interval", 30.0),
        "oi_history_size": getattr(st, "oi_history_size", 30),
        "oi_sample_count": getattr(st, "oi_sample_count", 3),
        "bleed_window_sec": getattr(st, "bleed_window_sec", 5.0),
        "equity_history_window_sec": getattr(st, "equity_history_window_sec", 30.0),
        "api_timeout": getattr(st, "api_timeout", 10),
        "health_stale_sec": getattr(st, "health_stale_sec", 30.0),
        "health_max_failures": getattr(st, "health_max_failures", 3),
        "coin_amplitude_24h": round(getattr(st, "coin_amplitude_24h", 0.0), 2),
        "dr_24h": round(getattr(st, "dr_24h", 0.0), 3),
        "dr_7d": round(getattr(st, "dr_7d", 0.0), 3),
        "use_dynamic_params": getattr(st, "use_dynamic_params", True),
        "target_spacing_pct": getattr(st, "target_spacing_pct", 0.60),
        "adj_ratio": getattr(st, "adj_ratio", 0.06),
        "grid_upper_px": px_round(getattr(st, "inst_id", ""), getattr(st, "grid_upper_px", 0.0)),
        "grid_lower_px": px_round(getattr(st, "inst_id", ""), getattr(st, "grid_lower_px", 0.0)),
        # 网格计算透明化（供前端计算器读真实值，替代写死残留）
        "atr_pct": round(float(getattr(st, "atr_pct", 0.0) or 0.0), 3),
        "atr_abs": round(float(getattr(st, "atr_abs", 0.0) or 0.0), 8),
        "grid_spacing_pct": round(float(getattr(st, "grid_spacing_pct", 0.0) or 0.0), 3),
        "current_density": round(float(getattr(st, "current_density", 0.0) or 0.0), 3),
        "grid_anchor_px": round(float(getattr(st, "grid_anchor_px", 0.0) or 0.0), 8),
    }
    d["adjust_history"] = getattr(st, "adjust_history", [])[-20:]
    d["calc_details"] = getattr(st, "calc_details", {})
    return d


def _adapter_ready() -> bool:
    """复刻旧版 is_adapter_ready()：行情客户端是否就绪。"""
    try:
        if _get_ticker_client is not None:
            _get_ticker_client()
            return True
    except Exception:
        pass
    return False


def _get_client():
    """复刻旧版 get_adapter()._client：返回公开行情客户端。"""
    if _get_ticker_client is not None:
        return _get_ticker_client()
    raise RuntimeError("no adapter")


# ══════════════════════════════════════════════════════════════════
#  委托/调平记录分组逻辑（数值全部来自交易所，与旧版一致）
# ══════════════════════════════════════════════════════════════════

def _classify_group(orders):
    """判定一组订单类型。orders: 同一调平动作的一笔或两笔

    规则（用户2026-08-11确认）：
      - 成对意图优先：平多开空 / 平空开多（组内部分撤销仍按意图归类，前端逐单拆成交/撤销）
      - 整组全部撤销 → canceled；shrink 须有真实成交，避免把拆散的 reduceOnly 虚算成纯缩仓
    """
    sides = {(o.get("side"), o.get("posSide"), o.get("reduceOnly", "false")) for o in orders}
    filled = [o for o in orders if o.get("state") in ("filled", "partially_filled")
              or _to_f(o.get("accFillSz")) > 0]
    all_canceled = all(o.get("state") == "canceled" for o in orders)
    # 成对意图按"成交单"判断(组内部分撤销不降级；避免把实际成交的平空开多误判成平多开空)
    filled_sides = {(o.get("side"), o.get("posSide"), o.get("reduceOnly", "false")) for o in filled}
    if {("sell", "long", "true"), ("sell", "short", "false")} <= filled_sides:
        return "pingduo_kaikong"
    if {("buy", "short", "true"), ("buy", "long", "false")} <= filled_sides:
        return "pingkong_kaiduo"
    if all_canceled:
        return "canceled"
    if filled and all(o.get("reduceOnly", "false") == "true" for o in filled):
        return "shrink"
    if ("buy", "long", "false") in sides and ("sell", "short", "false") in sides:
        return "build"
    return "other"


def _can_pair(a, b):
    """判断两笔订单是否能组成同一动作组（特征互补）"""
    pa = (a.get("side"), a.get("posSide"), a.get("reduceOnly", "false"))
    pb = (b.get("side"), b.get("posSide"), b.get("reduceOnly", "false"))
    pair = {pa, pb}
    if pair == {("buy", "long", "false"), ("sell", "short", "false")}:
        return True
    if pair == {("sell", "short", "false"), ("sell", "long", "true")}:
        return True
    if pair == {("buy", "long", "false"), ("buy", "short", "true")}:
        return True
    if pa[2] == "true" and pb[2] == "true" and pa != pb:
        return True
    return False


def _group_orders(orders):
    """按时间窗口(≤3000ms)把同一次调平触发的订单合并成一组。"""
    orders = sorted(orders, key=lambda o: _to_f(o.get("cTime")))
    groups, cur, last_ts = [], [], None
    for o in orders:
        ts = _to_f(o.get("cTime"))
        if cur and last_ts is not None and (ts - last_ts) > 3000:
            groups.append(cur)
            cur = []
        cur.append(o)
        last_ts = ts
    if cur:
        groups.append(cur)
    return groups


def _is_open(side, ps):
    """判断一单是开仓还是平仓。开仓=开多/开空，平仓=平多/平空。"""
    return (side == "buy" and ps == "long") or (side == "sell" and ps == "short")


def _build_record(orders, gtype):
    """把一组订单合成一条展示记录（数值全部来自交易所）。

    方向列按真实4方向(开多/平空/开空/平多)保留，已成交列拆分"开/平"张数。
    每条 detail 记录实际成交状态：组内有成交(filled)标为成交，全撤销才标撤销。
    """
    filled = [o for o in orders if o.get("state") != "canceled" and _to_f(o.get("accFillSz")) > 0]
    main = filled[0] if filled else orders[0]
    try:
        ct_val = get_state().ct_val
    except Exception:
        ct_val = 0.1
    if gtype == "canceled":
        avg_px = _to_f(main.get("px"))
        acc_fill = _to_f(main.get("sz"))
    else:
        avg_px = _to_f(main.get("avgPx"))
        acc_fill = _to_f(main.get("accFillSz"))
    notional = main.get("notionalUsd")
    value = _to_f(notional) if notional else (avg_px * acc_fill * ct_val)
    DIR_LABEL = {("buy", "long"): "买入开多", ("buy", "short"): "买入平空",
                 ("sell", "short"): "卖出开空", ("sell", "long"): "卖出平多"}
    CANCEL_SIDE_LABEL = {"sell": "🔴开空/平多", "buy": "🟢开多/平空"}

    def _detail_state(o):
        # 该笔是否真实成交
        if o.get("state") in ("filled", "partially_filled") or _to_f(o.get("accFillSz")) > 0:
            return "filled"
        return "canceled"

    # 按"方向侧(side)"合并：开/平分别累计张数(用户要求合并成🔴开空/平多、🟢开多/平空)
    dir_map = {}
    for o in orders:
        side = o.get("side")
        ps = o.get("posSide")
        px = _to_f(o.get("px"))
        sz = _to_f(o.get("sz"))
        st = _detail_state(o)
        key = side
        label = CANCEL_SIDE_LABEL.get(side, DIR_LABEL.get((side, ps), side))
        if key not in dir_map:
            dir_map[key] = {
                "side": side, "pos_side": ps,
                "dir_label": label,
                "px": px, "sz": 0.0, "open_sz": 0.0, "close_sz": 0.0,
                "value": 0.0, "state": st, "ord_type": o.get("ordType"),
            }
        d = dir_map[key]
        d["sz"] += sz
        d["value"] += px * sz * ct_val
        if _is_open(side, ps):
            d["open_sz"] += sz
        else:
            d["close_sz"] += sz
        # 同侧状态：只要有一笔成交 → 整体视为成交（真实成交优先）
        if st == "filled":
            d["state"] = "filled"
    details = list(dir_map.values())
    _detail_inst = main.get("instId", "")
    for d in details:
        d["value"] = round(d["value"], 2)
        d["open_sz"] = round(d["open_sz"], 4)
        d["close_sz"] = round(d["close_sz"], 4)
        d["px"] = px_round(_detail_inst, d.get("px", 0))
        d["tick_sz"] = _safe_tick_sz(_detail_inst)
    bidirectional = len(dir_map) >= 2
    return {
        "group": gtype,
        "inst_id": main.get("instId", ""),
        "side": main.get("side", ""),
        "ts": _to_f(main.get("cTime")),
        "time_str": _fmt_ms(_to_f(main.get("cTime"))),
        "avg_px": px_round(main.get("instId", ""), avg_px),
        "acc_fill_sz": acc_fill,
        "value": round(value, 2),
        "fee": round(sum(_to_f(o.get("fee")) for o in orders), 4),
        "state": main.get("state", ""),
        "ord_type": main.get("ordType", ""),
        "ord_ids": [o.get("ordId") for o in orders],
        "details": details,
        "bidirectional": bidirectional,
    }


def _serialize_adjust(st):
    """生成调平记录展示数据：seq 编号 + 统计条 + 最近3组"""
    stats = {"pingduo_kaikong": 0, "pingkong_kaiduo": 0, "shrink": 0, "canceled": 0}
    recs = []
    seq = 0
    bt = getattr(st, "build_ts", 0.0)
    for r in sorted(getattr(st, "adjust_records", []), key=lambda x: x.get("ts", 0)):
        # 记录 ts 为毫秒、build_ts 为秒，需统一单位再比较
        if bt > 0 and _to_f(r.get("ts", 0)) < bt * 1000.0:
            continue
        r = dict(r)
        g = r["group"]
        if g in ("pingduo_kaikong", "pingkong_kaiduo") and r.get("state") != "canceled" and r.get("acc_fill_sz", 0) > 0:
            seq += 1
            r["seq"] = seq
            stats[g] += 1
        elif g == "shrink":
            stats["shrink"] += 1
        elif g == "canceled":
            stats["canceled"] += 1
        recs.append(r)
    recs = list(reversed(recs))
    return {
        "records": recs,
        "recent": recs[:3],
        "total": len(recs),
        "stats": stats,
        "build_ts": getattr(st, "build_ts", 0.0),
        "inst_id": getattr(st, "inst_id", ""),
    }


def _recalc_formula_details(st):
    """更新 calc_details 用于前端公式展示（不改变持仓，仅计算）。"""
    try:
        px = get_mark_px() or getattr(getattr(st, "position", None), "mark_px", 0) or 0
        grid_cap = _grid_available(st)
        total_eq = getattr(st, "total_equity", 0) or 1
        max_per_side = 1
        if is_auth_ready and get_auth_client is not None:
            ac = get_auth_client()
            if is_auth_ready() and ac:
                try:
                    max_info = ac.get_max_size(getattr(st, "inst_id", ""))
                    max_buy = int(float(max_info.get("maxBuy", "0") or "0"))
                    max_sell = int(float(max_info.get("maxSell", "0") or "0"))
                    max_per_side = min(max_buy, max_sell) if max_buy > 0 and max_sell > 0 else max(max_buy, max_sell)
                except Exception:
                    pass
        # 对冲双开：每边张数 = 交易所最大(单边) ÷ 2，资金分给双边
        single_limit = int((max_per_side / 2) * (grid_cap / total_eq))
        safety_open = int(single_limit * getattr(st, "safety_factor", 0.7))
        cd = getattr(st, "calc_details", None)
        if cd is None:
            cd = {}
            try:
                setattr(st, "calc_details", cd)
            except Exception:
                pass
        cd["position_calc"] = {
            "formula": "单边极限 = 交易所最大 × (网格可用 ÷ 总权益)",
            "vars": f"交易所最大={max_per_side}张, 网格可用={grid_cap:.0f}USDT, 总权益={total_eq:.0f}USDT, 安全系数={getattr(st,'safety_factor',0.7)}",
            "result": f"单边极限={single_limit}张, 安全开仓={safety_open}张",
        }
        return cd
    except Exception:
        return None


def _fetch_ct_val(inst_id, simulated=False):
    """复刻旧版 _fetch_ct_val：从公开行情获取合约面值。失败返回 None。"""
    try:
        client = _get_client()
        inst = client.get_instrument(inst_id)
        if inst:
            v = inst.get("ctVal")
            if v:
                return _to_f(v)
    except Exception:
        pass
    # 兜底按价格区间估算面值
    try:
        tk = _get_client().get_ticker(inst_id)
        px = _to_f(tk.get("last") or tk.get("markPx"))
        if px > 50000:
            return 0.01
        if px > 1000:
            return 0.1
        if px > 10:
            return 1.0
        return 10.0
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
#  中间件（复刻旧版：/W2 前缀 + 禁用静态文件缓存）
# ══════════════════════════════════════════════════════════════════
class W2PrefixAndCacheMiddleware:
    """ASGI中间件：处理 /W2 前缀 + 禁用静态文件缓存"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "/")
            if path.startswith("/W2"):
                scope["path"] = path[3:] or "/"

            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = dict(message.get("headers", []))
                    ct = headers.get(b"content-type", b"").decode("latin-1")
                    if "text/html" in ct or "javascript" in ct or "text/css" in ct:
                        headers[b"cache-control"] = b"no-cache, no-store, must-revalidate"
                        headers[b"pragma"] = b"no-cache"
                        headers[b"expires"] = b"0"
                        message["headers"] = [(k, v) for k, v in headers.items()]
                await send(message)

            await self.app(scope, receive, send_wrapper)
        else:
            await self.app(scope, receive, send)


# ══════════════════════════════════════════════════════════════════
#  路由注册
# ══════════════════════════════════════════════════════════════════
def register_routes(app: FastAPI):
    """在 FastAPI app 上注册所有路由（返回 app）。"""

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(W2PrefixAndCacheMiddleware)

    # 静态目录（相对 backend_v2/）
    _static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
    try:
        from fastapi.staticfiles import StaticFiles
        app.mount("/static", StaticFiles(directory=_static_dir), name="static")
    except Exception as e:
        logger.warning(f"静态目录挂载失败: {e}")

    # ─── 健康检查 ───
    @app.get("/health")
    async def health():
        api_stats = (get_api_stats() if get_api_stats else {}) or {}
        now = time.time()
        last_success = api_stats.get("last_success_ts", 0)
        consecutive_failures = api_stats.get("consecutive_failures", 0)
        st = get_state()
        api_connected = (now - last_success < st.health_stale_sec) and consecutive_failures < st.health_max_failures
        return {
            "status": "ok",
            "version": "2.0.0",
            "okx_connected": _adapter_ready(),
            "api_connected": api_connected,
            "api_latency_ms": round(api_stats.get("avg_latency_ms", 0), 1),
            "api_consecutive_failures": consecutive_failures,
        }

    @app.get("/")
    async def root():
        return FileResponse(os.path.join(_static_dir, "v3.html"))

    # ─── 状态 ───
    @app.get("/api/v1/state")
    async def get_state_route():
        st = get_state()
        _recalc_formula_details(st)
        return {"status": "ok", "data": _state_dict(st)}

    # ─── 行情 ───
    @app.get("/api/v1/market")
    async def get_market(inst_id: str = ""):
        # 指定 inst_id 时：强制刷新该币行情并写入缓存，确保前端切币能立即拿到数据
        if inst_id and _force_ticker_refresh is not None:
            try:
                _force_ticker_refresh(inst_id)
            except Exception:
                pass
        enriched = []
        _available_inst_ids = get_available_inst_ids() if get_available_inst_ids else set()
        for t in get_ticker_cache() or []:
            if inst_id and t["inst_id"] != inst_id:
                continue
            if _available_inst_ids and t["inst_id"] not in _available_inst_ids:
                continue
            item = dict(t)
            item["change_7d_pct"] = _change_7d_cache.get(t["inst_id"], 0)
            oi = (get_oi_cache() or {}).get(t["inst_id"], {})
            if oi:
                item["open_interest"] = oi.get("oi", 0)
                item["oi_change_pct"] = round(get_oi_change(t["inst_id"]), 2)
            enriched.append(item)
        _mp = get_mark_px()
        return {"status": "ok", "data": {"tickers": enriched, "mark_px": _mp}}

    # ─── 交易对推荐 ───
    def _score_capital_fit(init_ct: float) -> float:
        if init_ct <= 0:
            return 0
        if init_ct < 3:
            return (init_ct / 3) * 40
        if init_ct <= 10:
            return 40 + (init_ct - 3) / 7 * 30
        if init_ct <= 50:
            return 70 + (init_ct - 10) / 40 * 30
        if init_ct <= 100:
            return 100 - (init_ct - 50) / 50 * 30
        return max(30, 70 - (init_ct - 100) / 100 * 40)

    def _score_amplitude(amp: float) -> float:
        if amp <= 0:
            return 10
        if amp < 1:
            return (amp / 1) * 15
        if amp <= 3:
            return 15 + (amp - 1) / 2 * 10
        if amp <= 8:
            return 25 + (1 - abs(amp - 5.5) / 2.5) * 10
        return max(5, 35 - (amp - 8) / 5 * 30)

    def _score_volume(vol_usdt: float) -> float:
        if vol_usdt <= 0:
            return 0
        if vol_usdt < 1_000_000:
            return (vol_usdt / 1_000_000) * 10
        if vol_usdt <= 10_000_000:
            return 10 + (vol_usdt - 1_000_000) / 9_000_000 * 10
        return min(25, 20 + (vol_usdt / 10_000_000) ** 0.3 * 2)

    @app.get("/api/v1/grid/pair-recommendations")
    async def pair_recommendations():
        st = get_state()
        grid_cap = _grid_available(st)
        leverage = getattr(st, "leverage", 20)
        safety = getattr(st, "safety_factor", 0.7)

        if grid_cap <= 0 or not get_ticker_cache():
            return {"status": "ok", "data": {"recommendations": [], "grid_available": round(grid_cap, 2)}}

        ct_val_map: dict = {}
        try:
            ct_val_map = _get_client()._ct_val_map
        except Exception:
            pass

        _available_inst_ids = get_available_inst_ids() if get_available_inst_ids else set()
        scored = []
        for t in get_ticker_cache() or []:
            inst_id = t["inst_id"]
            if _available_inst_ids and inst_id not in _available_inst_ids:
                continue

            px = t.get("last_px", 0)
            amp = t.get("amplitude_24h_pct", 0)
            chg = t.get("change_24h_pct", 0)
            vol = t.get("vol_ccy_24h", 0)

            if px <= 0:
                continue

            ct_val = ct_val_map.get(inst_id, 0)
            if ct_val <= 0:
                ct_val = _ct_val_cache.get(inst_id, 0)
            if ct_val <= 0:
                if px > 50000:
                    ct_val = 0.01
                elif px > 1000:
                    ct_val = 0.1
                elif px > 10:
                    ct_val = 1.0
                else:
                    ct_val = 10.0

            raw_ct = (grid_cap * leverage * safety) / (2 * ct_val * px)
            init_ct = max(raw_ct // 1, 1)
            capital_score = _score_capital_fit(init_ct)

            dr_24h = abs(chg) / amp if amp > 0 else 0.5
            dr_score = (1 - min(dr_24h, 1)) * 40
            amp_score = _score_amplitude(amp)
            vol_score = _score_volume(vol)
            grid_score = dr_score + amp_score + vol_score

            oi_change = get_oi_change(inst_id)
            oi_score = max(0, 50 - abs(oi_change) * 2.5)

            chg_7d = _change_7d_cache.get(inst_id, 0)
            trend_score = max(0, 50 - abs(chg_7d) * 2.0)

            stability_score = oi_score * 0.5 + trend_score * 0.5
            total = capital_score * 0.25 + grid_score * 0.50 + stability_score * 0.25

            scored.append({
                "inst_id": inst_id,
                "score": round(total, 1),
                "capital_score": round(capital_score, 1),
                "grid_score": round(grid_score, 1),
                "stability_score": round(stability_score, 1),
                "init_contracts": int(init_ct),
                "ct_val": ct_val,
                "dr_24h": round(dr_24h, 3),
                "amplitude_24h": round(amp, 1),
                "vol_24h_usdt": round(vol, 0),
                "change_24h": round(chg, 2),
                "change_7d": chg_7d,
                "last_px": px_round(inst_id, px),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        top10 = scored[:10]

        return {
            "status": "ok",
            "data": {
                "recommendations": top10,
                "grid_available": round(grid_cap, 2),
                "leverage": leverage,
                "total_analyzed": len(scored),
            }
        }

    # ─── 合约可用性 ───
    @app.get("/api/v1/instruments/available")
    async def get_available_instruments():
        st = get_state()
        _available_inst_ids = get_available_inst_ids() if get_available_inst_ids else set()
        return {"status": "ok", "data": {
            "count": len(_available_inst_ids),
            "mode": "simulated" if getattr(st, "simulated", False) else "live",
            "ids": sorted(list(_available_inst_ids))
        }}

    # ─── K线 ───
    @app.get("/api/v1/candles")
    async def get_candles(inst_id: str = "", bar: str = "15m", limit: int = 100):
        st = get_state()
        inst = inst_id or getattr(st, "inst_id", "")
        if not inst:
            return {"status": "error", "msg": "未选择交易对"}
        try:
            data = _get_client().get_candles(inst, bar, limit)
        except Exception as e:
            return {"status": "error", "msg": str(e)}
        return {"status": "ok", "data": data}

    # ─── 日志 ───
    @app.get("/api/v1/logs")
    async def get_logs(cat: str = None, level: str = None, limit: int = 200,
                       mech: str = None, src: str = None):
        diag = _diag()
        if diag is None:
            return {"status": "ok", "data": {"logs": [], "total": 0, "categories": {},
                                             "health": []}}
        # src=file 走文件(按日期)，否则走内存
        if src == "file":
            logs = diag.get_file_logs(date=(level or None), limit=limit)
        else:
            logs = diag.get_logs(cat=cat, level=level, limit=limit)
        if mech:
            logs = [e for e in logs if e.get("mech") == mech]
        return {"status": "ok", "data": {"logs": logs, "total": len(logs),
                                         "categories": diag.get_categories(),
                                         "health": diag.get_health()}}

    @app.get("/api/v1/logs/health")
    async def get_logs_health():
        diag = _diag()
        if diag is None:
            return {"status": "ok", "data": {"health": []}}
        return {"status": "ok", "data": {"health": diag.get_health(),
                                         "categories": diag.get_categories()}}

    @app.get("/api/v1/logs/download")
    async def download_logs(date: str = None):
        diag = _diag()
        if diag is None:
            return {"status": "error", "msg": "日志组件未就绪"}
        if date is None:
            path = diag.get_today_file()
        else:
            path = os.path.join(diag.log_dir, f"diag_{date}.jsonl")
        if not os.path.exists(path):
            return {"status": "error", "msg": "日志文件不存在"}
        return FileResponse(path, media_type="application/jsonl",
                            filename=f"diag_{date or 'today'}.jsonl")

    @app.get("/api/v1/logs/file")
    async def get_file_logs(date: str = None, limit: int = 500):
        diag = _diag()
        if diag is None:
            return {"status": "ok", "data": {"logs": [], "total": 0}}
        logs = diag.get_file_logs(date=date, limit=limit)
        return {"status": "ok", "data": {"logs": logs, "total": len(logs)}}

    # ─── OI ───
    @app.get("/api/v1/open-interest")
    async def get_open_interest(inst_id: str = "ETH-USDT-SWAP"):
        oi = (get_oi_cache() or {}).get(inst_id, {})
        return {"status": "ok", "data": {"inst_id": inst_id, **oi, "change_pct": round(get_oi_change(inst_id), 2)}}

    # ─── 余额 ───
    @app.get("/api/v1/balance")
    async def get_balance():
        _auth_client = get_auth_client() if get_auth_client else None
        if not is_auth_ready() or _auth_client is None:
            return {"status": "ok", "data": {"configured": False}}
        try:
            bal = _auth_client.get_account_balance()
            return {"status": "ok", "data": {"configured": True, "balance": bal}}
        except Exception as e:
            return {"status": "ok", "data": {"configured": True, "error": str(e)}}

    # ─── 网格预检 ───
    @app.get("/api/v1/grid/precheck")
    async def precheck_grid(inst_id: str = "", leverage: int = 0):
        st = get_state()
        inst = inst_id or getattr(st, "inst_id", "")
        lev = leverage or getattr(st, "leverage", 20)
        px = get_mark_px()

        result = {
            "inst_id": inst, "leverage": lev, "mark_px": px_round(inst, px),
            "formula_ct": 0, "max_ct": 0, "max_per_side": 0,
            "single_limit": 0, "safety_open": 0,
            "error": "",
        }

        if px <= 0:
            result["error"] = "无行情数据"
            return {"status": "ok", "data": result}

        ct_val = getattr(st, "ct_val", 0.1)
        grid_cap = _grid_available(st)
        total_eq = getattr(st, "total_equity", 0) or 1

        _auth_client = get_auth_client() if get_auth_client else None
        _auth_ready_val = is_auth_ready() if is_auth_ready else False
        if _auth_ready_val and _auth_client:
            try:
                max_info = _auth_client.get_max_size(inst, leverage=lev, px=px)
                max_buy = int(float(max_info.get("maxBuy", "0") or "0"))
                max_sell = int(float(max_info.get("maxSell", "0") or "0"))
                max_per_side = min(max_buy, max_sell) if max_buy > 0 and max_sell > 0 else max(max_buy, max_sell)
                result["max_ct"] = max(max_buy, max_sell)
                result["max_per_side"] = max_per_side
                result["max_detail"] = f"getMaxSize: maxBuy={max_buy}, maxSell={max_sell} → 每边最多{max_per_side}张"
            except Exception as e:
                result["error"] = f"查询限额失败: {e}"
                max_per_side = 1
        else:
            max_per_side = 1

        # 对冲双开：每边张数 = 交易所最大(单边) ÷ 2，资金分给双边
        single_limit = int((max_per_side / 2) * (grid_cap / total_eq))
        safety_open = int(single_limit * getattr(st, "safety_factor", 0.7))
        result["single_limit"] = max(single_limit, 1)
        result["safety_open"] = max(safety_open, 1)
        result["formula_ct"] = result["safety_open"]
        result["formula_detail"] = f"单边极限={result['single_limit']}张 (=({max_per_side}/2)×{grid_cap:.0f}/{total_eq:.0f}), 安全开仓={result['safety_open']}张 (×{getattr(st,'safety_factor',0.7)})"

        return {"status": "ok", "data": result}

    # ─── 杠杆选项 ───
    @app.get("/api/v1/grid/leverage-options")
    async def leverage_options(inst_id: str = ""):
        st = get_state()
        inst = inst_id or getattr(st, "inst_id", "")
        px = get_mark_px()
        _auth_client = get_auth_client() if get_auth_client else None
        if inst != getattr(st, "inst_id", "") and _auth_client:
            try:
                tk = _auth_client.get_ticker(inst)
                if tk.get("last"):
                    px = float(tk["last"])
            except Exception:
                pass

        result = {
            "inst_id": inst,
            "mark_px": px_round(inst, px),
            "current_leverage": getattr(st, "leverage", 20),
            "grid_capital": round(_grid_available(st), 2),
            "options": [],
            "source": "formula",
        }

        cache_key = f"{inst}:{_grid_available(st):.0f}"
        now = time.time()
        if cache_key in _lev_opt_cache and _lev_opt_cache_ts.get(cache_key, 0) > now - _LEV_OPT_CACHE_TTL:
            cached = _lev_opt_cache[cache_key]
            cached["current_leverage"] = getattr(st, "leverage", 20)
            cached["grid_capital"] = round(_grid_available(st), 2)
            cached["mark_px"] = px_round(inst, px)
            for o in cached.get("options", []):
                o["is_current"] = o.get("leverage") == getattr(st, "leverage", 20)
            cached["_cached"] = True
            return {"status": "ok", "data": cached}

        if px <= 0:
            return {"status": "ok", "data": result}

        if build_auth_client is not None:
            try:
                build_auth_client()
            except Exception:
                pass

        _auth_client = get_auth_client() if get_auth_client else None
        if (is_auth_ready() if is_auth_ready else False) and _auth_client:
            try:
                grid_cap = _grid_available(st)
                settle_ccy = "USDT"
                scale = 1.0
                try:
                    bal = _auth_client.get_account_balance()
                    for d in bal.get("details", []):
                        ccy = d.get("ccy", "")
                        if ccy in ("USDT", "USDC"):
                            settle_bal = float(d.get("availEq", "0") or "0")
                            if settle_bal > 0:
                                settle_ccy = ccy
                                result["settle_balance"] = round(settle_bal, 2)
                                scale = grid_cap / settle_bal if settle_bal > 0 else 1.0
                                result["scale"] = round(scale, 4)
                                break
                    else:
                        scale = 1.0
                except Exception:
                    scale = 1.0
                result["settle_ccy"] = settle_ccy

                tiers_max_lever = 125
                try:
                    inst_family = inst.replace("-SWAP", "")
                    tiers_data = _auth_client._request("GET", "/api/v5/public/position-tiers", {
                        "instType": "SWAP", "instFamily": inst_family, "tdMode": "cross",
                    }).get("data", [])
                    if tiers_data:
                        tiers_max_lever = int(float(tiers_data[0].get("maxLever", "125")))
                        result["tiers_max_lever"] = tiers_max_lever
                except Exception as e:
                    logger.debug(f"获取position-tiers失败（不影响后续）: {e}")

                valid_levers = [l for l in AVAILABLE_LEVERS if l <= tiers_max_lever]

                def _query_one(lever):
                    try:
                        max_info = _auth_client.get_max_size(inst, lever, px)
                    except Exception:
                        max_info = {}
                    max_buy = int(float(max_info.get("maxBuy", "0") or "0"))
                    max_sell = int(float(max_info.get("maxSell", "0") or "0"))
                    scaled_buy = max(int(max_buy * scale), 0)
                    scaled_sell = max(int(max_sell * scale), 0)
                    exchange_limit = min(scaled_buy, scaled_sell) if scaled_buy > 0 and scaled_sell > 0 else max(scaled_buy, scaled_sell)
                    return {
                        "leverage": lever,
                        "exchange_max_per_side": exchange_limit,
                        "exchange_max_buy": scaled_buy,
                        "exchange_max_sell": scaled_sell,
                        "is_current": lever == getattr(st, "leverage", 20),
                    }

                lever_map: dict = {}
                if valid_levers:
                    with ThreadPoolExecutor(max_workers=len(valid_levers)) as pool:
                        futures = {pool.submit(_query_one, lever): lever for lever in valid_levers}
                        for fut in as_completed(futures):
                            opt = fut.result()
                            lever_map[opt["leverage"]] = opt

                for lever in valid_levers:
                    if lever in lever_map:
                        result["options"].append(lever_map[lever])

                result["source"] = "exchange"
                _lev_opt_cache[cache_key] = result.copy()
                _lev_opt_cache_ts[cache_key] = now

            except Exception as e:
                logger.warning(f"逐档位查询限额失败: {e}")
                try:
                    max_info = _auth_client.get_max_size(inst)
                    max_buy = int(float(max_info.get("maxBuy", "0") or "0"))
                    max_sell = int(float(max_info.get("maxSell", "0") or "0"))
                    exchange_limit = min(max_buy, max_sell) if max_buy > 0 and max_sell > 0 else max(max_buy, max_sell)
                    result["exchange_limit"] = exchange_limit
                except Exception:
                    pass

        if not result["options"]:
            result["source"] = "unavailable"
            result["msg"] = "未连接交易所，无法获取实时限额"

        return {"status": "ok", "data": result}

    # ─── 杠杆滑块 ───
    @app.get("/api/v1/grid/lever-tiers")
    async def lever_tiers(inst_id: str = ""):
        st = get_state()
        inst = inst_id or getattr(st, "inst_id", "")

        px = get_mark_px()
        _auth_client = get_auth_client() if get_auth_client else None
        if inst != getattr(st, "inst_id", ""):
            try:
                if RawOkxRestClient is not None:
                    pub = RawOkxRestClient(timeout=5, simulated=getattr(st, "simulated", False))
                    tk = pub.get_ticker(inst)
                    if tk.get("last"):
                        px = float(tk["last"])
            except Exception:
                pass

        tiers_max_lever = 125
        try:
            inst_family = inst.replace("-SWAP", "")
            if RawOkxRestClient is not None:
                pub_client = RawOkxRestClient(timeout=10, simulated=getattr(st, "simulated", False))
                tiers_data = pub_client._request("GET", "/api/v5/public/position-tiers", {
                    "instType": "SWAP", "instFamily": inst_family, "tdMode": "cross",
                }).get("data", [])
                if tiers_data:
                    tiers_max_lever = int(float(tiers_data[0].get("maxLever", "125")))
        except Exception as e:
            logger.debug(f"lever-tiers: 获取position-tiers失败 {e}")

        valid_levers = [l for l in AVAILABLE_LEVERS if l <= tiers_max_lever]

        cur_sizing = {"exchange_max_per_side": 0, "exchange_max_buy": 0, "exchange_max_sell": 0}
        if (is_auth_ready() if is_auth_ready else False) and _auth_client and px > 0:
            try:
                scale = 1.0
                try:
                    bal = _auth_client.get_account_balance()
                    for d in bal.get("details", []):
                        if d.get("ccy", "") in ("USDT", "USDC"):
                            settle_bal = float(d.get("availEq", "0") or "0")
                            if settle_bal > 0:
                                scale = _grid_available(st) / settle_bal
                                break
                except Exception:
                    pass

                max_info = _auth_client.get_max_size(inst, getattr(st, "leverage", 20), px)
                max_buy = int(float(max_info.get("maxBuy", "0") or "0"))
                max_sell = int(float(max_info.get("maxSell", "0") or "0"))
                scaled_buy = max(int(max_buy * scale), 0)
                scaled_sell = max(int(max_sell * scale), 0)
                cur_sizing = {
                    "exchange_max_per_side": min(scaled_buy, scaled_sell) if scaled_buy > 0 and scaled_sell > 0 else max(scaled_buy, scaled_sell),
                    "exchange_max_buy": scaled_buy,
                    "exchange_max_sell": scaled_sell,
                }
            except Exception as e:
                logger.warning(f"lever-tiers: 获取当前杠杆张数失败 {e}")

        return {
            "status": "ok",
            "inst_id": inst,
            "mark_px": px_round(inst, px),
            "ct_val": getattr(st, "ct_val", 0.1),
            "grid_capital": round(_grid_available(st), 2),
            "max_lever": tiers_max_lever,
            "valid_levers": valid_levers,
            "current_lever": getattr(st, "leverage", 20),
            "current_sizing": cur_sizing,
            "auth_ready": (is_auth_ready() if is_auth_ready else False) and _auth_client is not None,
        }

    @app.get("/api/v1/grid/lever-size")
    async def lever_size(lever: int = 0, inst_id: str = ""):
        st = get_state()
        inst = inst_id or getattr(st, "inst_id", "")
        if lever <= 0:
            lever = getattr(st, "leverage", 20)

        _auth_client = get_auth_client() if get_auth_client else None
        if not (is_auth_ready() if is_auth_ready else False) or not _auth_client:
            return {"status": "error", "msg": "未连接交易所，无法获取实时限额"}

        px = get_mark_px()
        if inst != getattr(st, "inst_id", ""):
            try:
                tk = _auth_client.get_ticker(inst)
                if tk.get("last"):
                    px = float(tk["last"])
            except Exception:
                pass

        scale = 1.0
        try:
            bal = _auth_client.get_account_balance()
            for d in bal.get("details", []):
                if d.get("ccy", "") in ("USDT", "USDC"):
                    settle_bal = float(d.get("availEq", "0") or "0")
                    if settle_bal > 0:
                        scale = _grid_available(st) / settle_bal
                        break
        except Exception:
            pass

        try:
            max_info = _auth_client.get_max_size(inst, lever, px)
        except Exception:
            max_info = {}

        max_buy = int(float(max_info.get("maxBuy", "0") or "0"))
        max_sell = int(float(max_info.get("maxSell", "0") or "0"))
        scaled_buy = max(int(max_buy * scale), 0)
        scaled_sell = max(int(max_sell * scale), 0)
        exchange_limit = min(scaled_buy, scaled_sell) if scaled_buy > 0 and scaled_sell > 0 else max(scaled_buy, scaled_sell)

        return {
            "status": "ok",
            "lever": lever,
            "exchange_max_per_side": exchange_limit,
            "exchange_max_buy": scaled_buy,
            "exchange_max_sell": scaled_sell,
            "mark_px": px_round(inst, px),
            "source": "exchange",
        }

    # ─── 网格控制 ───
    @app.post("/api/v1/grid/start")
    async def start_grid(req: dict):
        contracts = int(req.get("contracts", 0))
        try:
            from engine.build import execute_start_grid
            r = execute_start_grid(contracts=contracts)
            if r.get("status") == "ok":
                try:
                    from notify import on_op
                    on_op("🔨 建仓/启动", f"{contracts}张")
                except Exception:
                    pass
            return r
        except Exception as e:
            return {"status": "error", "msg": f"建仓失败: {e}"}

    @app.post("/api/v1/grid/recover")
    async def recover_grid():
        """从交易所认领现有挂单并无损恢复运行（自愈式接管，无需重启）。"""
        st = get_state()
        try:
            from engine.grid import adopt_or_reset_grid_orders
            adopted = adopt_or_reset_grid_orders(st)
            st.running = True
            if save_state is not None:
                save_state()
            _add_log(st, f"🔁 已恢复接管（认领现有挂单: {adopted}）")
            try:
                from notify import on_status
                on_status("🔁 网格已恢复接管")
            except Exception:
                pass
            return {"status": "ok", "data": {"adopted": adopted}}
        except Exception as e:
            return {"status": "error", "msg": f"恢复接管失败: {e}"}

    @app.post("/api/v1/admin/reload-build")
    async def admin_reload_build():
        try:
            import engine.build
            importlib.reload(engine.build)
            return {"status": "ok", "msg": "engine.build reloaded"}
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    @app.post("/api/v1/grid/stop")
    async def stop_grid():
        get_state().running = False
        _add_log(get_state(), "⏹ 网格已暂停")
        try:
            from notify import on_status
            on_status("⏹ 网格已停止")
        except Exception:
            pass
        return {"status": "ok"}

    @app.post("/api/v1/grid/flat")
    async def flat_grid():
        st = get_state()
        st.running = False
        try:
            from engine.flat import execute_emergency
            await asyncio.to_thread(execute_emergency, st, "手动全平")
            try:
                from notify import on_op
                on_op("💥 全平(手动)")
            except Exception:
                pass
        except Exception as e:
            return {"status": "error", "msg": f"全平失败: {e}"}
        return {"status": "ok"}

    @app.post("/api/v1/grid/hedge")
    async def hedge_grid():
        try:
            from engine.hedge import execute_hedge
            r = execute_hedge()
            if r.get("status") == "ok":
                try:
                    from notify import on_op
                    on_op("🔒 对锁")
                except Exception:
                    pass
            return r
        except Exception as e:
            return {"status": "error", "msg": f"对锁失败: {e}"}

    @app.post("/api/v1/grid/cancel-iceberg")
    async def cancel_iceberg():
        st = get_state()
        st.pending_iceberg = 0
        _add_log(st, "🗑 冰山单已撤销")
        return {"status": "ok"}

    @app.post("/api/v1/grid/adjust")
    async def manual_adjust():
        try:
            from engine.adjust import check_adjust
            check_adjust(get_state())
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "msg": f"调平失败: {e}"}

    @app.get("/api/v1/trend")
    async def get_trend():
        try:
            from engine.trend import get_trend_info, force_refresh, _should_refresh, _trend_cache
            st = get_state()
            inst_id = getattr(st, "inst_id", "")
            if inst_id and (_should_refresh(inst_id) or _trend_cache.get("inst_id") != inst_id):
                force_refresh(inst_id)
            info = get_trend_info()
            return {"status": "ok", "data": info}
        except Exception as e:
            return {"status": "ok", "data": {}}

    @app.get("/api/v1/adapter")
    async def get_adapter_data():
        """供前端L2行情条：ticker缓存 + OI"""
        st = get_state()
        mk = get_ticker_cache() or []
        oi = get_oi_cache() or {}
        oi_chg = get_oi_change(getattr(st, "inst_id", ""))
        tickers = []
        for t in mk:
            inst = t.get("inst_id", "")
            inst_lower = inst.lower() if inst else ""
            oi_data = oi.get(inst, oi.get(inst_lower, {}))
            tk = {
                "inst_id": inst,
                "last_px": t.get("last", 0),
                "idx_px": t.get("idxPx", 0),
                "high_24h": t.get("high24h", 0),
                "low_24h": t.get("low24h", 0),
                "vol_24h": t.get("vol24h", 0),
                "vol_ccy_24h": t.get("volCcy24h", 0),
                "amplitude_24h_pct": t.get("amplitude_24h_pct", 0),
                "change_24h_pct": t.get("change_24h_pct", 0),
                "open_interest": oi_data.get("oi", 0),
                "oi_change_pct": round(oi_chg, 2) if oi_chg else 0,
                "change_7d_pct": t.get("change_7d_pct", 0),
                "mark_px": get_mark_px() if inst == getattr(st, "inst_id", "") else t.get("markPx", 0),
            }
            tickers.append(tk)
        return {"status": "ok", "data": {"tickers": tickers, "current_inst": getattr(st, "inst_id", "")}}

    @app.post("/api/v1/trend/refresh")
    async def refresh_trend():
        try:
            from engine.trend import force_refresh
            st = get_state()
            info = force_refresh(getattr(st, "inst_id", ""), getattr(st, "simulated", False))
            return {"status": "ok", "data": info}
        except Exception as e:
            return {"status": "ok", "data": {}}

    @app.post("/api/v1/grid/margin-mode")
    async def set_margin_mode(req: dict):
        st = get_state()
        mode = req.get("mode", "usdt")
        if mode not in ("usdt", "coin"):
            return {"status": "error", "msg": "mode must be usdt or coin"}
        if mode == getattr(st, "margin_mode", "usdt"):
            return {"status": "ok", "margin_mode": getattr(st, "margin_mode", "usdt")}
        old_mode = getattr(st, "margin_mode", "usdt")
        if getattr(st, "running", False):
            st.running = False
            try:
                from engine.flat import execute_emergency
                await asyncio.to_thread(execute_emergency, st, f"切换{mode}")
            except Exception as e:
                logger.warning(f"切换{mode}全平失败: {e}")
        st.margin_mode = mode
        base = getattr(st, "inst_id", "").replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
        st.inst_id = f"{base}-USDT-SWAP" if mode == "usdt" else f"{base}-USD-SWAP"
        cv = _fetch_ct_val(st.inst_id, getattr(st, "simulated", False))
        if cv is not None:
            st.ct_val = cv
        else:
            st.ct_val = 0.1 if mode == "usdt" else 10.0
        _add_log(st, f"🔄 {base}: {'U本位' if mode == 'usdt' else '币本位'} (面值={st.ct_val})")
        return {"status": "ok", "margin_mode": st.margin_mode, "inst_id": st.inst_id, "ct_val": st.ct_val}

    @app.get("/api/v1/grid/margin-mode")
    async def get_margin_mode():
        st = get_state()
        return {"status": "ok", "margin_mode": getattr(st, "margin_mode", "usdt"), "ct_val": getattr(st, "ct_val", 0.1), "inst_id": getattr(st, "inst_id", "")}

    @app.post("/api/v1/grid/instid")
    async def set_inst_id(req: dict):
        sid = req.get("inst_id", "")
        st = get_state()
        if not sid or sid == getattr(st, "inst_id", ""):
            return {"status": "ok", "inst_id": getattr(st, "inst_id", "")}

        mode = "模拟盘" if getattr(st, "simulated", False) else "实盘"
        _available_inst_ids = get_available_inst_ids() if get_available_inst_ids else set()
        if _available_inst_ids and sid not in _available_inst_ids:
            msg = f"{sid} 在{mode}不可用，请切换到实盘" if getattr(st, "simulated", False) else f"{sid} 在当前模式不可用"
            return {"status": "error", "msg": msg}

        loop = asyncio.get_event_loop()
        cv_fut = loop.run_in_executor(None, _fetch_ct_val, sid, getattr(st, "simulated", False))
        try:
            cv = await asyncio.gather(cv_fut)[0] if False else await cv_fut
        except Exception:
            cv = None

        if cv is None:
            logger.warning(f"切换交易对失败: 无法获取{sid}的合约信息")
            return {"status": "error", "msg": f"无法获取{sid}的合约信息，请检查网络连接"}

        was_running = getattr(st, "running", False)
        if was_running:
            st.running = False
            try:
                from engine.flat import execute_emergency
                await asyncio.to_thread(execute_emergency, st, f"切换交易对{sid}")
            except Exception as e:
                logger.warning(f"切换交易对{sid}全平失败: {e}")

        old_inst = getattr(st, "inst_id", "")
        st.inst_id = sid
        st.ct_val = cv

        try:
            from models import GridPosition
            st.position = GridPosition(inst_id=sid)
            if hasattr(st.position, "set_margin_params"):
                st.position.set_margin_params(st.ct_val, getattr(st, "leverage", 20))
        except Exception:
            try:
                st.position = type(getattr(st, "position"))(inst_id=sid)
            except Exception:
                pass

        st.grid_upper_px = 0.0
        st.grid_lower_px = 0.0

        if save_state is not None:
            try:
                save_state()
            except Exception:
                pass
        _add_log(st, f"📌 交易对切换: {old_inst} → {sid}")

        _auth_client = get_auth_client() if get_auth_client else None
        if (is_auth_ready() if is_auth_ready else False) and _auth_client:
            loop.run_in_executor(None, _auth_client.set_leverage, sid, getattr(st, "leverage", 20), "cross")

        return {"status": "ok", "inst_id": st.inst_id, "ct_val": st.ct_val}

    # ─── 账户/杠杆 ───
    @app.post("/api/v1/account/set-leverage")
    async def set_account_leverage(req: dict):
        st = get_state()
        lever = int(req.get("leverage", getattr(st, "leverage", 20)))
        st.leverage = lever
        _auth_client = get_auth_client() if get_auth_client else None
        if (is_auth_ready() if is_auth_ready else False) and _auth_client:
            try:
                lr = _auth_client.set_leverage(getattr(st, "inst_id", ""), lever, "cross")
                if lr.get("code") == "0":
                    _add_log(st, f"⚙️ 杠杆已设置: {getattr(st,'inst_id','')} {lever}x", cat="TRADE")
                    return {"status": "ok", "leverage": lever, "inst_id": getattr(st, "inst_id", "")}
                return {"status": "error", "msg": lr.get("msg", "")}
            except Exception as e:
                return {"status": "error", "msg": str(e)}
        return {"status": "error", "msg": "API未配置"}

    @app.get("/api/v1/account/leverage")
    async def get_account_leverage():
        st = get_state()
        _auth_client = get_auth_client() if get_auth_client else None
        if (is_auth_ready() if is_auth_ready else False) and _auth_client:
            try:
                info = _auth_client.get_leverage_info(getattr(st, "inst_id", ""), "cross")
                return {"status": "ok", "data": info}
            except Exception as e:
                return {"status": "error", "msg": str(e)}
        return {"status": "error", "msg": "API未配置"}

    @app.post("/api/v1/grid/demo-mode")
    async def set_demo_mode(req: dict):
        st = get_state()
        enabled = req.get("enabled", False)
        if enabled == getattr(st, "simulated", False):
            mode = "模拟盘" if enabled else "实盘"
            return {"status": "ok", "simulated": getattr(st, "simulated", False), "msg": f"已经是{mode}"}
        if getattr(st, "running", False):
            st.running = False
            _add_log(st, "⚠️ 切换模式：网格已自动停止", "WARN", "STATE")
        old_mode = "模拟盘" if getattr(st, "simulated", False) else "实盘"
        st.simulated = enabled
        new_mode = "模拟盘" if enabled else "实盘"
        if build_auth_client is not None:
            try:
                build_auth_client()
            except Exception:
                pass
        if refresh_available_instruments is not None:
            try:
                refresh_available_instruments()
            except Exception:
                pass
        # ── 切换模式清残留账（只清"当前运行快照"，保留历史记录）──
        # 清持仓：避免实盘界面残留模拟盘持仓，等正确 client sync 拉回实盘真实持仓
        pos = getattr(st, "position", None)
        if pos is not None:
            pos.long_contracts = 0
            pos.short_contracts = 0
            pos.long_avg_px = 0.0
            pos.short_avg_px = 0.0
            pos.long_unrealized_pnl = 0.0
            pos.short_unrealized_pnl = 0.0
            pos.long_liq_px = 0
            pos.short_liq_px = 0
            pos.long_be_px = 0
            pos.short_be_px = 0
        # 清统计（变0 → 触发 backfill_grid_stats 从对应模式订单历史自动重算）
        st.grid_count = 0
        st.total_pnl = 0.0
        st.total_fee = 0.0
        st.imbalance_rate = 0.0
        # total_equity 不清：让正确 client 的 sync_equity 自然覆盖，避免 capital 空窗变 1
        if save_state is not None:
            try:
                save_state()
            except Exception:
                pass
        _add_log(st, f"🔄 模式切换: {old_mode} → {new_mode}", "INFO", "STATE",
                 {"old": old_mode, "new": new_mode})
        logger.info(f"模式切换: {old_mode} → {new_mode}")
        return {"status": "ok", "simulated": getattr(st, "simulated", False), "mode": new_mode}

    # ─── API Key ───
    @app.get("/api/v1/config/apikey")
    async def get_apikey():
        cfg = load_apikey() if load_apikey else {}
        st = get_state()
        if "simulated" in cfg or "live" in cfg:
            s = cfg.get("simulated", {})
            l = cfg.get("live", {})
        else:
            s = cfg if getattr(st, "simulated", False) else {}
            l = {} if getattr(st, "simulated", False) else cfg

        def _mask(v):
            if not v:
                return ""
            s_v = str(v)
            if len(s_v) <= 8:
                return s_v[:2] + "…" + s_v[-2:]
            return s_v[:4] + "…" + s_v[-4:]

        active = s if getattr(st, "simulated", False) else l
        return {"status": "ok", "data": {
            "simulated_configured": bool(s.get("api_key")),
            "live_configured": bool(l.get("api_key")),
            "current_mode": "simulated" if getattr(st, "simulated", False) else "live",
            "api_key_masked": _mask(active.get("api_key")),
            "api_secret_masked": _mask(active.get("api_secret")),
            "passphrase_masked": _mask(active.get("passphrase")),
        }}

    @app.post("/api/v1/config/apikey")
    async def set_apikey(req: dict):
        existing = load_apikey() if load_apikey else {}
        st = get_state()
        mode = req.get("mode", "simulated" if getattr(st, "simulated", False) else "live")
        keys = {}
        for k in ["api_key", "api_secret", "passphrase"]:
            if req.get(k):
                keys[k] = req[k]
        if not keys:
            return {"status": "error", "msg": "未提供有效key"}
        if "simulated" not in existing and "live" not in existing:
            existing = {"simulated": {}, "live": {}}
        existing[mode] = {**existing.get(mode, {}), **keys}
        if save_apikey is not None:
            save_apikey(existing)
        if build_auth_client is not None:
            try:
                build_auth_client()
            except Exception:
                pass
        # 存 key 后同步重建 data/exchange 与 data/ticker 的 client，
        # 确保新 key 立即生效于持仓/余额/行情链路
        try:
            from data.exchange import build_auth_client as _dex_build
            _dex_build()
        except Exception:
            pass
        try:
            from data.ticker import init_adapter as _dtk_init
            _dtk_init()
        except Exception:
            pass
        logger.info(f"API密钥已保存（{mode}）")
        return {"status": "ok", "mode": mode}

    # ─── 参数 ───
    @app.post("/api/v1/config/params")
    async def set_params(req: dict):
        st = get_state()
        calc_recalc = False
        for k in ["capital", "reserved_capital", "leverage", "imbalance_threshold",
                  "iceberg_sz", "pxVar", "auto_adjust", "use_iceberg",
                  "use_risk_control", "use_bleed_melt", "use_rebalance",
                  "safety_factor", "shrink_pct", "oi_spike_pct", "oi_drop_pct",
                  "oi_lock_base", "bleed_threshold_pct",
                  "price_offset_pct", "adjust_split_ratio", "adj_ratio",
                  "target_spacing_pct", "imbalance_blowup_pct", "rebalance_target_pct",
                  "tp_base_pct", "tp_window_hours",
                  "data_loop_interval", "oi_full_refresh_interval", "oi_history_size",
                  "oi_sample_count", "bleed_window_sec", "equity_history_window_sec",
                  "api_timeout", "health_stale_sec", "health_max_failures",
                  "use_dynamic_params", "base_density", "defense_density", "mode",
                  "one_way_threshold", "ladder_rates", "ladder_densities",
                  "confirm_time", "exit_buffer", "debounce_loss_line",
                  "debounce_profit_line", "atr_timeframe", "atr_period", "density_min",
                  "cumulative_drawdown_threshold", "safety_flat_threshold",
                  "trend_tf", "ema_fast", "ema_slow", "st_period", "st_mult",
                  "oi_n"]:
            if k in req:
                old_val = getattr(st, k, None)
                new_val = req[k]
                if k == "reserved_capital":
                    new_val = float(new_val)
                    if new_val < 0:
                        new_val = 0.0
                setattr(st, k, new_val)
                if old_val != new_val:
                    d = _diag()
                    if d is not None:
                        try:
                            d.info("PARAM", f"参数变更: {k} = {old_val} → {new_val}",
                                   {"param": k, "old": old_val, "new": new_val})
                        except Exception:
                            pass
                    if k in ("reserved_capital", "leverage", "safety_factor",
                             "mmr", "zone_warning_ratio", "zone_risk_ratio", "capital"):
                        calc_recalc = True
        if calc_recalc:
            _recalc_formula_details(st)
        # 改动6：网格间距相关参数变化 → 保存后立即撤单重挂（撤单已带安全保险）
        # 触发条件：网格密度/调仓比例/ATR/单向阈值等影响挂单价的参数变了，且引擎运行中
        grid_params = {"adj_ratio", "base_density", "defense_density", "density_min",
                       "atr_timeframe", "atr_period", "one_way_threshold",
                       "ladder_rates", "ladder_densities"}
        grid_changed = any(k in req for k in grid_params)
        if grid_changed:
            try:
                from engine.grid import place_grid_orders
                if getattr(st, "running", False) and not getattr(st, "paused", False):
                    place_grid_orders(st)
                    _add_log(st, "⚙ 网格参数已更新 → 已撤旧单并按新参数重挂", cat="PARAM")
            except Exception as e:
                _add_log(st, f"⚠ 参数更新后重挂失败: {e}", level="ERROR", cat="PARAM")
        _add_log(st, "⚙ 参数已更新", cat="PARAM")
        if save_state is not None:
            try:
                save_state()
            except Exception:
                pass
        return {"status": "ok"}

    @app.post("/api/v1/grid/add-capital")
    async def add_capital(req: dict):
        st = get_state()
        amount = float(req.get("amount", 0))
        if amount <= 0:
            return {"status": "error", "msg": "追加金额必须大于0"}
        old_reserved = getattr(st, "reserved_capital", 0.0)
        st.reserved_capital = max(getattr(st, "reserved_capital", 0.0) - amount, 0)
        actual_added = old_reserved - st.reserved_capital
        _add_log(st, f"💰 追加本金: {actual_added:.2f} USDT → 网格可用 {_grid_available(st):.2f}",
                 cat="PARAM", data={"amount": amount, "actual_added": actual_added,
                                    "reserved": st.reserved_capital, "available": _grid_available(st)})
        if save_state is not None:
            try:
                save_state()
            except Exception:
                pass
        return {"status": "ok", "reserved": st.reserved_capital, "grid_available": _grid_available(st)}

    @app.post("/api/v1/grid/pause")
    async def pause_grid(req: dict):
        st = get_state()
        paused = req.get("paused", True)
        st.paused = paused
        _add_log(st, "⏸️ 策略已挂起" if paused else "▶️ 策略已恢复")
        return {"status": "ok", "paused": st.paused}

    @app.get("/api/v1/grid/pause")
    async def get_pause_status():
        st = get_state()
        return {"status": "ok", "paused": getattr(st, "paused", False)}

    @app.get("/api/v1/adapter/ready")
    async def adapter_info():
        return {"ready": _adapter_ready()}

    # ═══ 委托记录 & 调平记录（拉交易所真实数据）═══
    @app.get("/api/v1/trade/pending")
    async def trade_pending():
        st = get_state()
        cli = get_auth_client() if get_auth_client else None
        if cli is None:
            return {"status": "ok", "data": []}
        try:
            pend = cli.get_orders_pending(getattr(st, "inst_id", ""))
        except Exception as e:
            return {"status": "error", "msg": str(e), "data": []}
        ct_val = getattr(st, "ct_val", 0.1)
        rows = []
        for o in pend:
            px = _to_f(o.get("px"))
            sz = _to_f(o.get("sz"))
            notional = o.get("notionalUsd")
            val = _to_f(notional) if notional else px * sz * ct_val
            rows.append({
                "inst_id": o.get("instId"),
                "time_str": _fmt_ms(_to_f(o.get("cTime"))),
                "side": o.get("side"),
                "pos_side": o.get("posSide"),
                "px": px, "sz": sz,
                "tick_sz": _safe_tick_sz(o.get("instId", "")),
                "acc_fill_sz": _to_f(o.get("accFillSz")),
                "value": round(val, 2),
                "state": o.get("state"),
                "ord_type": o.get("ordType"),
                "ord_id": o.get("ordId"),
            })
        return {"status": "ok", "data": rows}

    @app.get("/api/v1/trade/adjust-history")
    async def adjust_history_route():
        st = get_state()
        cli = get_auth_client() if get_auth_client else None
        if cli is not None:
            try:
                hist = cli.get_order_history(getattr(st, "inst_id", ""), limit=100)
            except Exception:
                hist = []
            # 方案B：全量按cTime窗口重建记录（幂等/自愈；分批成交时张数与手续费每轮重算跟最新）
            records = []
            for g in _group_orders(hist or []):
                gtype = _classify_group(g)
                if gtype == "build":
                    continue
                records.append(_build_record(g, gtype))
            st.adjust_records = records[-300:]
        return {"status": "ok", "data": _serialize_adjust(st)}

    # ═══ 企微通知配置（前端可配置：长链接/群推送/大模型）═══
    @app.get("/api/v1/wecom/config")
    async def wecom_config_get():
        try:
            return {"status": "ok", "data": wecom_config.get_public_status()}
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    @app.post("/api/v1/wecom/config")
    async def wecom_config_post(payload: dict):
        try:
            cfg = wecom_config.save_config(payload)
            return {"status": "ok", "data": wecom_config.get_public_status()}
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    @app.post("/api/v1/wecom/test")
    async def wecom_config_test(payload: dict = None):
        import urllib.request
        kind = (payload or {}).get("kind", "webhook")
        try:
            if kind == "webhook":
                url = wecom_config.get_value("wecom_webhook_url")
                if not url:
                    return {"status": "error", "msg": "未配置群推送 webhook URL"}
                req = urllib.request.Request(url, data=json.dumps(
                    {"msgtype": "markdown", "markdown": {"content": "✅ grid-V2.0 配置测试成功"}}).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return {"status": "ok", "resp": r.read().decode("utf-8", "ignore")[:200]}
            elif kind == "llm":
                key = wecom_config.get_value("openrouter_api_key")
                model = wecom_config.get_value("wecom_query_model")
                if not key:
                    return {"status": "error", "msg": "未配置大模型 APIKEY"}
                req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                    data=json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}).encode("utf-8"),
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return {"status": "ok", "resp": r.read().decode("utf-8", "ignore")[:200]}
            elif kind == "longlink":
                bot = wecom_config.get_value("wecom_bot_id")
                sec = wecom_config.get_value("wecom_bot_secret")
                return {"status": "ok",
                        "msg": ("已配置长链接，凭据变更后 notifier 约 15 秒内自动重连" if bot and sec else "未配置长链接"),
                        "bot_id_has": bool(bot), "secret_has": bool(sec)}
            return {"status": "error", "msg": f"未知测试类型 {kind}"}
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    return app
