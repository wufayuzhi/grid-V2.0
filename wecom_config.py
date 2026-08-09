"""
wecom_config.py — 企微通知配置读写（前端可配置）

配置来源优先级：/app/data-run/wecom_config.json（前端保存）> 环境变量（fallback）

统一管理 3 类企微通知配置：
  - wecom_bot_id / wecom_bot_secret   企业微信智能机器人长链接（私聊查询，双向）
  - wecom_webhook_url                  群机器人 webhook（群推送，单向）
  - openrouter_api_key / wecom_query_model   大模型 APIKEY + 模型（AI 回复）

凭证只落盘 volume 运行时数据区（/app/data-run/），不入库、不随镜像。
"""
import os
import json
import time

CONFIG_FILE = os.environ.get("WECOM_CONFIG_FILE", "/app/data-run/wecom_config.json")

DEFAULTS = {
    "wecom_bot_id": "",
    "wecom_bot_secret": "",
    "wecom_webhook_url": "",
    "openrouter_api_key": "",
    "wecom_query_model": "openai/gpt-oss-20b:free",
}

# key 名 → 环境变量 fallback 名
_ENV_MAP = {
    "wecom_bot_id": "WECOM_BOT_ID",
    "wecom_bot_secret": "WECOM_BOT_SECRET",
    "wecom_webhook_url": "WECOM_WEBHOOK_URL",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "wecom_query_model": "WECOM_QUERY_MODEL",
}


def load_config():
    """读取全部配置（合并默认值）。"""
    cfg = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    merged = dict(DEFAULTS)
    merged.update(cfg or {})
    return merged


def save_config(new_cfg):
    """保存配置到文件（部分更新）。返回合并后的完整配置。"""
    cfg = load_config()
    for k in DEFAULTS:
        if k in new_cfg:
            cfg[k] = new_cfg[k]
    cfg["updated_at"] = time.time()
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise RuntimeError(f"保存配置失败: {e}")
    return cfg


def get_value(key):
    """取单个配置值：优先文件，fallback 环境变量。"""
    if key not in DEFAULTS:
        return ""
    cfg = load_config()
    v = cfg.get(key)
    if v is None or str(v).strip() == "":
        env = os.environ.get(_ENV_MAP.get(key, ""), "")
        if env:
            return env
        return DEFAULTS.get(key, "")
    return v


def get_public_status():
    """返回前端可显示的状态（不回显完整 secret，只回打码 + 是否已配置）。"""
    cfg = load_config()

    def mask(v):
        if not v:
            return ""
        s = str(v)
        return s[:4] + "***" + (s[-2:] if len(s) > 6 else "")

    return {
        "wecom_bot_id": cfg.get("wecom_bot_id", ""),
        "wecom_bot_secret_has": bool(cfg.get("wecom_bot_secret")),
        "wecom_bot_secret_masked": mask(cfg.get("wecom_bot_secret")),
        "wecom_webhook_url_has": bool(cfg.get("wecom_webhook_url")),
        "wecom_webhook_url_masked": mask(cfg.get("wecom_webhook_url")),
        "openrouter_api_key_has": bool(cfg.get("openrouter_api_key")),
        "openrouter_api_key_masked": mask(cfg.get("openrouter_api_key")),
        "wecom_query_model": cfg.get("wecom_query_model", "openai/gpt-oss-20b:free"),
        "updated_at": cfg.get("updated_at"),
        "config_file": CONFIG_FILE,
    }
