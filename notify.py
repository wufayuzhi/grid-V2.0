#!/usr/bin/env python3
"""
grid-V2.0 群 webhook 常规推送
事件 → 格式化 markdown → POST 到企业微信群机器人 webhook
节流防刷屏: 同类事件最小间隔秒
凭证: WECOM_WEBHOOK_URL (env 或 .env.wecom 文件, 不入库)
"""
import json, os, time, urllib.request

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.wecom")

def _load_webhook_url():
    url = os.environ.get("WECOM_WEBHOOK_URL", "")
    if url:
        return url
    try:
        with open(_ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("WECOM_WEBHOOK_URL="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""

WEBHOOK_URL = _load_webhook_url()

# 同类事件最小间隔(秒): trade成交/imbalance失衡/risk风控/bleed失血/op操作/status启停
_MIN_INTERVAL = {"trade": 10, "imbalance": 60, "risk": 30, "bleed": 15, "op": 5, "status": 5}
_last = {}

def _throttle(cat):
    now = time.time()
    if now - _last.get(cat, 0) < _MIN_INTERVAL.get(cat, 0):
        return True
    _last[cat] = now
    return False

def _post(md):
    if not WEBHOOK_URL:
        return "no-webhook-url"
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": md}}).encode("utf-8")
    req = urllib.request.Request(WEBHOOK_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception as e:
        return f"ERR {e}"

def notify(cat, title, lines):
    """推送到群 webhook。lines: 消息内容行列表"""
    if _throttle(cat):
        return "throttled"
    md = f"**{title}**\n" + "\n".join(f"> {l}" for l in lines)
    return _post(md)

# ---- 预置各事件便捷函数 ----
def on_trade(direction, qty, price, equity, long_qty, short_qty):
    """成交: 平多/开空 或 平空/开多 完成"""
    return notify("trade", f"🔔 成交 | {direction} {qty}张 @ {price}",
                  [f"权益 {equity}", f"多 {long_qty} / 空 {short_qty}"])

def on_imbalance(rate, threshold, side):
    """失衡率过高 → 启动单边防御"""
    return notify("imbalance", "⚠️ 启动单边防御",
                  [f"失衡率 {rate}% ≥ 阈值 {threshold}%", f"生效侧: {side}"])

def on_risk_close(distance, equity):
    """离风控太近"""
    return notify("risk", "🛡️ 接近风控",
                  [f"剩余空间 {distance}", f"权益 {equity}"])

def on_bleed(speed, equity):
    """高速失血"""
    return notify("bleed", "🔴 高速失血",
                  [f"失血速度 {speed}", f"当前权益 {equity}"])

def on_op(op_name, detail=""):
    """重大操作完成"""
    return notify("op", f"🧩 {op_name}", [detail] if detail else [])

def on_status(status):
    """启停/重启恢复"""
    return notify("status", f"⏯️ {status}", [])

if __name__ == "__main__":
    import sys
    # 自测
    print(on_trade("平空/开多", 2, "1879.7", "4803.6", "157", "157"))
    print("节流测试(1分钟内同类不重复):", on_trade("平空/开多", 1, "1880", "4803", "157", "157"))
