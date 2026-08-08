#!/usr/bin/env python3
"""
grid-V2.0 企业微信智能机器人 长链接(WebSocket) 客户端
- 订阅 wss://openws.work.weixin.qq.com (BotID + Secret)
- 接收 aibot_msg_callback 入站消息 → 打印 + 缓存会话
- 主动推送 aibot_send_msg → 发交易通知
- 心跳 ping + 断线自动重连
凭证来自环境变量(不入库): WECOM_BOT_ID / WECOM_BOT_SECRET
"""
import asyncio, json, os, sys, time, signal
from datetime import datetime

try:
    import websockets
except ImportError:
    print("缺少 websockets，执行: pip install websockets>=12.0")
    sys.exit(1)

import query  # 私聊只读查询模组

WS_URL = "wss://openws.work.weixin.qq.com"
BOT_ID = os.environ.get("WECOM_BOT_ID", "")
SECRET = os.environ.get("WECOM_BOT_SECRET", "")
SESSION_FILE = os.environ.get("WECOM_SESSION_FILE", "/app/data/wecom_session.json")  # 记住会话
LOG_FILE = os.environ.get("WECOM_LOG_FILE", "/app/data/wecom_link.log")  # 原始消息日志

_state = {"connected": False, "req_seq": 0, "convos": {}}  # convos: conversation_id -> info

def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def next_req_id():
    _state["req_seq"] += 1
    return f"req_{int(time.time()*1000)}_{_state['req_seq']}"

def reply_via_url(response_url, content):
    """企业微信智能机器人官方回复机制：HTTP POST 到回调的 response_url"""
    import urllib.request
    payload = json.dumps({"msgtype": "text", "text": {"content": content}}).encode("utf-8")
    req = urllib.request.Request(response_url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return f"HTTP {r.status} {r.read().decode('utf-8', 'ignore')[:200]}"
    except Exception as e:
        return f"ERR {e}"

def load_session():
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_session():
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(_state["convos"], f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"保存会话失败: {e}")

def handle_frame(raw):
    """解析任意长链接帧，学习真实协议 + 记录会话"""
    try:
        msg = json.loads(raw)
    except Exception:
        log(f"[RAW] {raw[:500]}")
        return
    cmd = msg.get("cmd")
    body = msg.get("body") or {}
    headers = msg.get("headers") or {}

    if cmd == "aibot_subscribe":
        code = body.get("code")
        log(f"[订阅结果] code={code} msg={body.get('msg')} | {'✅ 订阅成功' if code==0 else '❌ 失败'}")
        if code == 0:
            _state["connected"] = True
            # 恢复已知会话
            for cid, info in load_session().items():
                _state["convos"][cid] = info
            log(f"已恢复已知会话: {list(_state['convos'].keys())}")
    elif cmd == "aibot_msg_callback":
        # 入站用户消息
        log(f"[入站完整帧] {json.dumps(msg, ensure_ascii=False)[:800]}")
        conv_id = body.get("conversation_id") or body.get("chatid") or body.get("conversationId") or body.get("to") or ""
        from_user = body.get("from") or body.get("from_user") or {}
        from_userid = from_user.get("userid") if isinstance(from_user, dict) else str(from_user)
        resp_url = body.get("response_url") or ""
        content = body.get("text") or {}
        text = content.get("content") if isinstance(content, dict) else str(content)
        log(f"[入站] userid={from_userid} chattype={body.get('chattype')} 内容={str(text)[:100]}")
        # 记录已知用户(用于主动推送)
        if from_userid:
            _state["convos"][from_userid] = {"chattype": body.get("chattype"), "last_msg": str(text)[:100], "ts": time.time()}
            save_session()
        # 用 response_url 回复(查询模组回答, 只读)
        if resp_url:
            try:
                reply_text = query.answer(str(text)) if text and text.strip() else "收到，请提问"
            except Exception as e:
                reply_text = f"❌ 查询模组异常: {e}"
            ack = reply_via_url(resp_url, reply_text[:1500])
            log(f"[查询回复] {reply_text[:80]}")
            log(f"[回复结果] {ack}")
        return None
    elif cmd == "aibot_event_callback":
        log(f"[事件] {json.dumps(body, ensure_ascii=False)[:300]}")
    elif cmd == "pong" or (cmd == "ping"):
        return None  # ping/pong 自动处理
    else:
        log(f"[其他帧] cmd={cmd} {json.dumps(msg, ensure_ascii=False)[:300]}")
    return None

async def heartbeat(ws):
    while True:
        await asyncio.sleep(25)
        try:
            await ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": next_req_id()}, "body": {}}))
        except Exception as e:
            log(f"心跳失败: {e}")

async def run():
    if not BOT_ID or not SECRET:
        log("❌ 缺少 WECOM_BOT_ID / WECOM_BOT_SECRET")
        return
    while True:
        try:
            log(f"连接长链接 {WS_URL} ...")
            async with websockets.connect(WS_URL, ping_interval=None, max_size=8*1024*1024) as ws:
                _state["connected"] = False
                sub = {"cmd": "aibot_subscribe",
                       "headers": {"req_id": next_req_id()},
                       "body": {"bot_id": BOT_ID, "secret": SECRET}}
                await ws.send(json.dumps(sub))
                hb = asyncio.create_task(heartbeat(ws))
                async for raw in ws:
                    reply = handle_frame(raw)
                    if reply:
                        try:
                            await ws.send(json.dumps(reply))
                        except Exception as e:
                            log(f"回复失败: {e}")
                hb.cancel()
        except asyncio.CancelledError:
            log("已停止")
            return
        except Exception as e:
            log(f"连接异常: {e}")
        log("3 秒后重连 ...")
        await asyncio.sleep(3)

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log("退出")
