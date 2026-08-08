#!/usr/bin/env python3
"""
grid-V2.0 私聊只读查询模组
- 用 OpenRouter 免费模型解释自然语言
- 只读工具白名单(全部 GET, 无任何写操作)
- 工具调用循环 → 查 localhost:8001 只读 API → 组织中文回答
凭证: OPENROUTER_API_KEY (来自 .env.wecom, 不入库)
"""
import json, os, urllib.request, urllib.parse

API_BASE = os.environ.get("GRID_API", "http://localhost:8001")
MODEL = os.environ.get("WECOM_QUERY_MODEL", "openai/gpt-oss-20b:free")
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OR_URL = "https://openrouter.ai/api/v1/chat/completions"

def _fetch(endpoint):
    """只读 GET 本地 grid API"""
    url = API_BASE + endpoint
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "grid-v2.0-query"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}

# ---- 只读工具白名单 (全部 GET) ----
TOOLS = [
    {"type": "function", "function": {
        "name": "get_state", "description": "查询 grid 核心状态: 权益、多空持仓、失衡率、运行状态、单边极限等",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_balance", "description": "查询账户余额/可用",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_market", "description": "查询当前行情(最新价/标记价等)",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_trend", "description": "查询趋势/失衡相关数据",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_pending", "description": "查询当前挂单/委托",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_adjust_history", "description": "查询最近调平/成交记录",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]

def _call_tool(name):
    return {
        "get_state": _fetch("/api/v1/state"),
        "get_balance": _fetch("/api/v1/balance"),
        "get_market": _fetch("/api/v1/market"),
        "get_trend": _fetch("/api/v1/trend"),
        "get_pending": _fetch("/api/v1/trade/pending"),
        "get_adjust_history": _fetch("/api/v1/trade/adjust-history"),
    }.get(name, {"error": "unknown tool"})

SYSTEM = (
    "你是 grid-V2.0 对冲网格系统的只读查询助手。用户会用中文问你交易状态相关问题。"
    "你只能通过提供的只读工具获取数据(工具只读,无任何操作能力)。"
    "根据工具返回的数据,用简洁中文回答用户。如果数据里没有用户要的信息,如实说明,不要编造。"
    "禁止给出任何交易建议,只陈述事实数据。回答控制在几行内。"
)

def _or_chat(messages, tools=None):
    body = {"model": MODEL, "messages": messages, "max_tokens": 500}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(OR_URL, data=data, headers={
        "Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}

def answer(user_text):
    """自然语言 → 只读工具调用循环 → 中文回答"""
    if not OR_KEY:
        return "❌ 查询模组未配置 OPENROUTER_API_KEY"
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_text}]
    for _ in range(5):  # 最多 5 轮工具调用
        resp = _or_chat(messages, TOOLS)
        if "error" in resp:
            return f"❌ 模型调用失败: {resp['error'][:200]}"
        choice = resp.get("choices", [{}])[0].get("message", {})
        tool_calls = choice.get("tool_calls")
        if not tool_calls:
            return (choice.get("content") or "").strip() or "（无回答）"
        # 执行工具调用
        messages.append({"role": "assistant", "content": choice.get("content") or "",
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            tid = tc.get("id", name)
            result = _call_tool(name)
            messages.append({"role": "tool", "tool_call_id": tid,
                             "content": json.dumps(result, ensure_ascii=False)[:3000]})
    return "（查询轮次过多，请换个问法）"

if __name__ == "__main__":
    import sys
    print(answer(sys.argv[1] if len(sys.argv) > 1 else "现在权益和持仓怎么样？"))
