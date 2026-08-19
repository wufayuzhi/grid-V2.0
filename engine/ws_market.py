"""
engine/ws_market.py — 模块A：OKX 行情实时获取（只读）。

架构（用户2026-08-19确认「WS 主推 + REST 并线」）：
  · 行情 WS（主通道，毫秒级）→ tickers/index-tickers/mark-price/open-interest → 更新统一缓存
  · K线（candle）走 REST 每秒增量拉取 + 缓存（实测: OKX 此环境 WS candle 频道 60018 不可用；
    公开K线接口限流宽(20次/2s)，每秒1次安全不429；行情走WS毫秒级）
  · 断线：指数退避重连（1s→2s→4s→…上限30s），重连后自动重新订阅
  · 切币/切tf：每秒检测 st.inst_id / st.ws_candle_tf 变化 → 断开重连重订阅 + K线重首拉

只读铁律：仅订阅公共频道，绝不下单/撤单/平仓/改状态。
"""
from __future__ import annotations
import asyncio
import json
import logging
import time

import websockets

from state import get_state
from data import ticker as T

logger = logging.getLogger("ws_market")

# OKX 公共行情 WS 端点（只读）
WS_URL = "wss://ws.okx.com:8443/ws/v5/public"

# 行情订阅频道（candle 已确认此环境 WS 不可用，走 REST 每秒增量）
_SUBSCRIBE_CHANNELS = ["tickers", "index-tickers", "mark-price", "open-interest"]

# 每秒检测切币/切tf 的轮询间隔
_INST_CHECK_INTERVAL = 1.0
# K线 REST 每秒增量拉取的根数(公开接口限流宽,安全)
_CANDLE_INC = 5
# K线每秒增量间隔
_CANDLE_INTERVAL = 1.0


def _build_subscribe_msgs(inst: str) -> list[dict]:
    """构造订阅消息：4 个行情频道（candle 走 REST，不入 WS）。"""
    args = [{"channel": ch, "instId": inst} for ch in _SUBSCRIBE_CHANNELS]
    return [{"op": "subscribe", "args": args}]


def _dispatch(inst: str, msg: dict) -> None:
    """按频道分发 WS 推送 → 更新统一缓存。"""
    try:
        ch = msg.get("arg", {}).get("channel", "")
        data = msg.get("data") or []
        for d in data:
            if ch == "tickers":
                T.apply_ws_ticker(inst, d)
            elif ch == "mark-price":
                T.apply_ws_mark_px(inst, d)
            elif ch == "index-tickers":
                T.apply_ws_idx_px(inst, d)
            elif ch == "open-interest":
                T.apply_ws_oi(inst, d)
    except Exception as ex:
        logger.warning(f"_dispatch({msg.get('arg')}): {ex}")



async def _ws_loop(running: asyncio.Event) -> None:
    """WS 主循环：连接→订阅→收推；断线指数退避重连；每秒检测切币/切tf重订阅。"""
    backoff = 1
    while not running.is_set():
        try:
            st = get_state()
            inst = st.inst_id
            tf = getattr(st, "ws_candle_tf", "1m") or "1m"
            if not inst:
                await asyncio.sleep(1)
                continue
            # 连接后发送订阅（candle 由 _candle_refresh_loop 走 REST 每秒增量）
            async with websockets.connect(
                WS_URL, ping_interval=20, ping_timeout=20, max_size=None
            ) as ws:
                for m in _build_subscribe_msgs(inst):
                    await ws.send(json.dumps(m))
                logger.info(f"WS已连接并订阅: {inst} tf={tf}")
                backoff = 1  # 连接成功重置退避
                while not running.is_set():
                    # 切币/切tf 检测：变化则断开外层重连（重连时自动重订阅+首拉K线）
                    st2 = get_state()
                    inst2 = st2.inst_id
                    tf2 = getattr(st2, "ws_candle_tf", "1m") or "1m"
                    if inst2 != inst or tf2 != tf:
                        logger.info(f"WS 切币/切tf: {inst}/{tf} → {inst2}/{tf2}")
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=_INST_CHECK_INTERVAL)
                    except asyncio.TimeoutError:
                        continue  # 无推送，回到切币检测
                    except Exception:
                        break  # 连接断开 → 外层重连
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("event") == "error":
                        logger.warning(f"WS error: {msg}")
                        continue
                    arg_inst = msg.get("arg", {}).get("instId", inst)
                    _dispatch(arg_inst, msg)
        except asyncio.CancelledError:
            break
        except Exception as ex:
            logger.warning(f"WS连接异常({backoff}s后重连): {ex}")
            try:
                await asyncio.wait_for(running.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30)


async def _candle_refresh_loop(running: asyncio.Event,
                               interval: float = _CANDLE_INTERVAL) -> None:
    """K线实时：每秒 REST 拉当前合约当前tf最近K线，增量更新缓存。
    公开K线接口限流宽(20次/2s)，每秒1次安全不429；行情已走WS毫秒级。
    OKX get_candles 返回倒序(新在前) → reversed 后逐根 apply_ws_candle 增量更新(按ts去重/追加)。"""
    from data.ticker import get_client
    while not running.is_set():
        try:
            st = get_state()
            inst = st.inst_id
            tf = getattr(st, "ws_candle_tf", "1m") or "1m"
            if inst:
                raw = await asyncio.to_thread(get_client().get_candles, inst, tf, _CANDLE_INC)
                if raw:
                    if not T.get_candle_cache(inst, tf):
                        T.init_candle_cache(inst, tf)  # 启动/切tf首拉历史K线
                    for d in reversed(raw):
                        T.apply_ws_candle(inst, d, tf)
        except asyncio.CancelledError:
            break
        except Exception as ex:
            logger.warning(f"_candle_refresh_loop: {ex}")
        try:
            await asyncio.wait_for(running.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def start_ws_task(loop: asyncio.AbstractEventLoop) -> asyncio.Task:
    """在事件循环中启动行情任务：WS 循环(行情毫秒级) + K线每秒增量循环(REST)。"""
    running = asyncio.Event()
    loop.create_task(_ws_loop(running))
    return loop.create_task(_candle_refresh_loop(running))
