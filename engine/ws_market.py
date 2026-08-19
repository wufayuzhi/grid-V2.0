"""
engine/ws_market.py — 模块A：OKX WebSocket 公共频道行情订阅（只读）。

架构（用户2026-08-19确认「WS + 低频REST 并线」）：
  · WS 实时推送（主通道，毫秒级）→ 更新 data/ticker 统一缓存
  · 低频 REST 5s 并线兜底（由 tick.py 的 REST 轮询维持，WS断线不中断）
  · 断线：指数退避重连（1s→2s→4s→…上限30s），重连后自动重新订阅 + REST 首拉K线
  · 切币/切tf：每秒检测 st.inst_id / st.ws_candle_tf 变化 → 断开重连重订阅

只读铁律：仅订阅公共频道（tickers/index-tickers/mark-price/open-interest/candle），
绝不下单/撤单/平仓/改状态。
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

# 当前合约订阅的 4 个行情频道（candle 单独按当前 tf 追加）
_SUBSCRIBE_CHANNELS = ["tickers", "index-tickers", "mark-price", "open-interest"]

# 每秒检测切币/切tf 的轮询间隔
_INST_CHECK_INTERVAL = 1.0


def _build_subscribe_msgs(inst: str, tf: str) -> list[dict]:
    """构造订阅消息：4 个行情频道 + candle(当前 tf)。"""
    args = [{"channel": ch, "instId": inst} for ch in _SUBSCRIBE_CHANNELS]
    args.append({"channel": "candle" + tf, "instId": inst})
    return [{"op": "subscribe", "args": args}]


def _dispatch(inst: str, msg: dict, tf: str) -> None:
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
            elif ch.startswith("candle"):
                T.apply_ws_candle(inst, d, tf)
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
            # 连接前 REST 首拉历史K线初始化缓存（启动/切币/切tf后）
            T.init_candle_cache(inst, tf)
            async with websockets.connect(
                WS_URL, ping_interval=20, ping_timeout=20, max_size=None
            ) as ws:
                for m in _build_subscribe_msgs(inst, tf):
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
                    _dispatch(arg_inst, msg, tf)
        except asyncio.CancelledError:
            break
        except Exception as ex:
            logger.warning(f"WS连接异常({backoff}s后重连): {ex}")
            try:
                await asyncio.wait_for(running.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30)


def start_ws_task(loop: asyncio.AbstractEventLoop) -> asyncio.Task:
    """在事件循环中启动 WS 后台任务。"""
    running = asyncio.Event()
    return loop.create_task(_ws_loop(running))
