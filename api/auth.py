"""
api/auth.py — 登录鉴权模块（grid-V2.0 网页登录 + 游客只读）

角色：
  - admin  : 管理员（读 + 写全部操作）— 登录账号密码获得
  - guest  : 游客（只读，任何写操作一律 403）— 点击"游客进入"获得

token 用 secrets.token_hex 生成，存内存 dict，带过期时间。
管理员凭据存环境变量 ADMIN_USER / ADMIN_PASSWORD（启动时读），
修改账号/密码时写回 config/auth_credentials.json 持久化。
"""
from __future__ import annotations

import os
import json
import time
import hmac
import secrets
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────
#  配置（环境变量优先，其次 config/auth_credentials.json）
# ────────────────────────────────────────────────────────────
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "auth_credentials.json")

_DEFAULT_ADMIN_USER = "tianjinsga"
_DEFAULT_ADMIN_PASS = "TJsga791106!"

TOKEN_TTL = 12 * 3600  # token 有效期 12 小时


def _load_config() -> dict:
    """读取持久化凭据（config/auth_credentials.json），不存在则用默认值。"""
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data or {}
    except Exception as e:
        logger.warning(f"auth_credentials.json 读取失败: {e}")
    return {}


def _save_config(data: dict) -> None:
    """持久化凭据到 config/auth_credentials.json（权限 600）。"""
    try:
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(_CONFIG_PATH, 0o600)
    except Exception as e:
        logger.warning(f"auth_credentials.json 写入失败: {e}")


def _current_admin() -> dict:
    """当前管理员凭据：环境变量 > 持久化文件 > 默认值。"""
    cfg = _load_config()
    user = os.environ.get("ADMIN_USER") or cfg.get("username") or _DEFAULT_ADMIN_USER
    pwd = os.environ.get("ADMIN_PASSWORD") or cfg.get("password") or _DEFAULT_ADMIN_PASS
    return {"username": user, "password": pwd}


def _constant_time_eq(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(str(a), str(b))
    except Exception:
        return a == b


# ────────────────────────────────────────────────────────────
#  Token 管理（内存）
# ────────────────────────────────────────────────────────────
_TOKENS: dict[str, dict] = {}  # {token: {"role": str, "exp": float}}


def _issue_token(role: str) -> str:
    """签发 token（带过期）。清理过期 token 防止无限增长。"""
    now = time.time()
    expired = [t for t, info in _TOKENS.items() if info["exp"] < now]
    for t in expired:
        _TOKENS.pop(t, None)
    token = secrets.token_hex(24)
    _TOKENS[token] = {"role": role, "exp": now + TOKEN_TTL}
    return token


def _validate_token(token: str) -> str | None:
    """校验 token，返回角色；无效/过期返回 None。"""
    if not token:
        return None
    info = _TOKENS.get(token)
    if not info:
        return None
    if info["exp"] < time.time():
        _TOKENS.pop(token, None)
        return None
    return info["role"]


def revoke_token(token: str) -> None:
    _TOKENS.pop(token, None)


def get_token_role(request: Request) -> str | None:
    """从请求 header 取 token 并返回角色。"""
    token = request.headers.get("x-auth-token", "") or request.headers.get("authorization", "").replace("Bearer ", "")
    return _validate_token(token)


def _is_write_method(method: str) -> bool:
    return method.upper() in ("POST", "PUT", "DELETE", "PATCH")


# ════════════════════════════════════════════════════════════
#  AuthMiddleware — 全局鉴权（游客只读强校验）
# ════════════════════════════════════════════════════════════
class AuthMiddleware(BaseHTTPMiddleware):
    """API 鉴权：
      - /api/v1/auth/*           → 免鉴权（登录/游客/改密）
      - 读操作(GET)              → 需有效 token（admin 或 guest）
      - 写操作(POST等)           → 需 admin token，guest 一律 403
      - 非 /api/ 请求(静态/登录页) → 放行
    """

    # 免鉴权路径前缀
    FREE_PREFIXES = ("/api/v1/auth/", "/api/v1/auth/login", "/api/v1/auth/guest",
                     "/api/v1/logs/frontend")  # 前端错误上报：未登录时也可能发生，需免鉴权
    # 静态/登录页放行
    PASS_PREFIXES = ("/static/", "/static", "/W2/", "/W2", "/favicon.ico", "/health")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        # 归一化：剥掉 /W2 前缀（W2Prefix 中间件后执行，此处需自行处理）
        norm = path[3:] if path.startswith("/W2") else path

        # 1) 免鉴权接口
        if norm.startswith("/api/v1/auth/"):
            return await call_next(request)

        # 2) 静态 / 前端 / 健康检查 → 放行
        if not norm.startswith("/api/"):
            return await call_next(request)
        if norm in ("/health",) or norm.startswith("/health"):
            return await call_next(request)

        # 3) API 需要 token
        role = get_token_role(request)
        if role is None:
            return JSONResponse({"status": "error", "msg": "未登录或登录已过期"}, status_code=401)

        # 4) 写操作：仅 admin
        if _is_write_method(method) and role != "admin":
            return JSONResponse({"status": "error", "msg": "游客模式只读，无写操作权限"}, status_code=403)

        return await call_next(request)


# ════════════════════════════════════════════════════════════
#  鉴权路由注册
# ════════════════════════════════════════════════════════════
def register_auth_routes(app) -> None:
    """注册登录/游客/改密码/改账号/登出 接口。"""
    from fastapi.responses import JSONResponse
    from fastapi import Body

    @app.post("/api/v1/auth/login")
    async def auth_login(payload: dict = Body(...)):
        """管理员登录：{username, password} → admin token"""
        cred = _current_admin()
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        if _constant_time_eq(username, cred["username"]) and _constant_time_eq(password, cred["password"]):
            token = _issue_token("admin")
            return {"status": "ok", "role": "admin", "token": token, "msg": "登录成功"}
        return JSONResponse({"status": "error", "msg": "账号或密码错误"}, status_code=401)

    @app.post("/api/v1/auth/guest")
    async def auth_guest():
        """游客进入 → guest token（只读）"""
        token = _issue_token("guest")
        return {"status": "ok", "role": "guest", "token": token, "msg": "游客模式（只读）"}

    @app.post("/api/v1/auth/logout")
    async def auth_logout(request: Request):
        token = request.headers.get("x-auth-token", "")
        if token:
            revoke_token(token)
        return {"status": "ok", "msg": "已退出"}

    @app.post("/api/v1/auth/change-password")
    async def auth_change_password(request: Request, payload: dict = Body(...)):
        """管理员改密码：{old_password, new_password}。仅 admin 有效。"""
        role = get_token_role(request)
        if role != "admin":
            return JSONResponse({"status": "error", "msg": "仅管理员可修改密码"}, status_code=403)
        cred = _current_admin()
        old_pwd = str(payload.get("old_password", ""))
        new_pwd = str(payload.get("new_password", ""))
        if not _constant_time_eq(old_pwd, cred["password"]):
            return JSONResponse({"status": "error", "msg": "原密码错误"}, status_code=401)
        if len(new_pwd) < 6:
            return JSONResponse({"status": "error", "msg": "新密码至少 6 位"}, status_code=400)
        # 持久化
        cfg = _load_config()
        cfg["password"] = new_pwd
        _save_config(cfg)
        logger.info("管理员已修改密码")
        return {"status": "ok", "msg": "密码修改成功"}

    @app.post("/api/v1/auth/change-username")
    async def auth_change_username(request: Request, payload: dict = Body(...)):
        """管理员改登录名：{password, new_username}。仅 admin 有效。"""
        role = get_token_role(request)
        if role != "admin":
            return JSONResponse({"status": "error", "msg": "仅管理员可修改登录名"}, status_code=403)
        cred = _current_admin()
        pwd = str(payload.get("password", ""))
        new_user = str(payload.get("new_username", ""))
        if not _constant_time_eq(pwd, cred["password"]):
            return JSONResponse({"status": "error", "msg": "密码错误"}, status_code=401)
        if not new_user or len(new_user) < 3:
            return JSONResponse({"status": "error", "msg": "登录名至少 3 位"}, status_code=400)
        cfg = _load_config()
        cfg["username"] = new_user
        _save_config(cfg)
        logger.info(f"管理员已修改登录名: {new_user}")
        return {"status": "ok", "msg": "登录名修改成功"}

    @app.get("/api/v1/auth/status")
    async def auth_status(request: Request):
        """查询当前登录状态/角色。"""
        role = get_token_role(request)
        if role is None:
            return {"status": "ok", "role": "anon"}
        return {"status": "ok", "role": role}
