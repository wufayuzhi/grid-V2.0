"""OKX REST API 客户端 — httpx HTTP/2 直连 www.okx.com

DNS 解析优先使用 dig @1.1.1.1 绕过 LXD DNS 毒化。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

_OKX_DOMAINS = {"www.okx.com", "openapi.okx.com", "ws.okx.com"}
_API_DOMAIN = "www.okx.com"
_API_PORT = 443
_TIMEOUT = 20                # 公共API超时秒（无state依赖时的默认值）
_MAX_RETRIES = 3             # API最大重试次数
_RETRY_DELAY_MAP = {"50011": 3, "50001": 3, "50026": 3, "50013": 60}

# API 连接状态追踪
_api_stats = {
    "last_latency_ms": 0,      # 最近一次 API 调用延迟（毫秒）
    "avg_latency_ms": 0,       # 滚动平均延迟
    "success_count": 0,        # 成功次数
    "fail_count": 0,           # 失败次数
    "last_success_ts": 0,      # 最近成功时间戳
    "last_fail_ts": 0,         # 最近失败时间戳
    "consecutive_failures": 0, # 连续失败次数
}
_latency_history: list[float] = []  # 最近 20 次延迟记录

def get_api_stats() -> dict:
    """获取 API 连接状态统计"""
    return _api_stats.copy()


# ── dig @1.1.1.1 DNS 解析（绕开 LXD DNS 毒化）───────────────

def _dig_resolve(domain: str) -> str | None:
    """用 dig @1.1.1.1 解析域名，绕开 LXD DNS 毒化"""
    try:
        result = subprocess.run(
            ["dig", "@1.1.1.1", domain, "+short"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and not line.endswith("."):
                try:
                    socket.inet_aton(line)
                    logger.debug("dig @1.1.1.1 %s -> %s", domain, line)
                    return line
                except OSError:
                    continue
    except FileNotFoundError:
        logger.warning("dig not found, fallback to system DNS")
    except Exception as e:
        logger.debug("dig @1.1.1.1 failed: %s", e)
    return None


# ── 拦截 socket.getaddrinfo 让 httpx 走 dig DNS ────────────

_ORIG_GETADDRINFO = socket.getaddrinfo

def _patched_getaddrinfo(
    host: str, port: int,
    family: int = 0, type_: int = 0, proto: int = 0, flags: int = 0,
) -> list[tuple]:
    """对 OKX 域名先用 dig @1.1.1.1 解析，失败回退系统 DNS"""
    if host in _OKX_DOMAINS:
        ip = _dig_resolve(host)
        if ip:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]
    return _ORIG_GETADDRINFO(host, port, family, type_, proto, flags)

socket.getaddrinfo = _patched_getaddrinfo


# ── httpx HTTP/2 客户端（同步版）───────────────────────────

_HTTP2_CLIENT: httpx.Client | None = None


def _get_http2_client() -> httpx.Client:
    """获取/创建 httpx HTTP/1.1 同步客户端（www.okx.com 不走代理时更稳定）"""
    global _HTTP2_CLIENT
    if _HTTP2_CLIENT is None:
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=50)
        _HTTP2_CLIENT = httpx.Client(
            http2=False,        # Cloudflare 对代理线路 HTTP/2 有限制，HTTP/1.1 更稳定
            timeout=_TIMEOUT,
            limits=limits,
            verify=True,
        )
    return _HTTP2_CLIENT


# ── 主类 ─────────────────────────────────────────────────

class RawOkxRestClient:
    """OKX REST API 客户端 — httpx HTTP/2 直连，dig @1.1.1.1 绕开 DNS 毒化"""

    def __init__(
        self,
        domain: str = _API_DOMAIN,
        port: int = _API_PORT,
        timeout: int = _TIMEOUT,
        simulated: bool = False,
    ):
        self.domain = domain
        self.port = port
        self.timeout = timeout
        self.simulated = simulated  # True=模拟盘, False=实盘

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> dict[str, Any]:
        """httpx HTTP/1.1 同步请求（带API降级重试，POST支持JSON body）"""
        client = _get_http2_client()
        url = f"https://{self.domain}{path}"
        t0 = time.time()
        # 降级映射（使用模块级常量）
        max_retries = _MAX_RETRIES
        for attempt in range(max_retries + 1):
            try:
                headers = {"User-Agent": "Hermes/1.0"}
                # 模拟盘：添加 x-simulated-trading header
                if self.simulated:
                    headers["x-simulated-trading"] = "1"

                # ── OKX V5 API 签名（有凭证时自动签名）──
                api_key = getattr(self, 'api_key', None)
                api_secret = getattr(self, 'api_secret', None)
                passphrase = getattr(self, 'passphrase', None)
                if api_key and api_secret and passphrase:
                    now = datetime.now(timezone.utc)
                    timestamp = now.strftime('%Y-%m-%dT%H:%M:%S.') + f"{now.microsecond // 1000:03d}Z"
                    # 构建签名字符串
                    if method == "GET":
                        body_str = ("?" + urlencode(params)) if params else ""
                    else:
                        if body is not None:
                            body_str = json.dumps(body, separators=(',', ':'))
                        elif params is not None:
                            body_str = json.dumps(params, separators=(',', ':'))
                        else:
                            body_str = ""
                    prehash = timestamp + method.upper() + path + body_str
                    signature = base64.b64encode(
                        hmac.new(api_secret.encode('utf-8'),
                                 prehash.encode('utf-8'),
                                 hashlib.sha256).digest()
                    ).decode()
                    headers["OK-ACCESS-KEY"] = api_key
                    headers["OK-ACCESS-SIGN"] = signature
                    headers["OK-ACCESS-TIMESTAMP"] = timestamp
                    headers["OK-ACCESS-PASSPHRASE"] = passphrase

                kwargs = {"headers": headers}
                if method == "GET":
                    kwargs["params"] = params
                elif body is not None:
                    kwargs["json"] = body
                elif params is not None:
                    kwargs["json"] = params
                response = client.request(method, url, **kwargs)
                latency_ms = (time.time() - t0) * 1000
                logger.debug("HTTP/2 %s %s (%.0fms)", method, path, latency_ms)

                # 记录成功
                if response.status_code < 400:
                    result = response.json()
                    if result.get("code") == "0":
                        _api_stats["last_latency_ms"] = latency_ms
                        _api_stats["success_count"] += 1
                        _api_stats["last_success_ts"] = time.time()
                        _api_stats["consecutive_failures"] = 0
                        # 更新滚动平均（最近 20 次）
                        _latency_history.append(latency_ms)
                        if len(_latency_history) > 20:
                            _latency_history.pop(0)
                        _api_stats["avg_latency_ms"] = sum(_latency_history) / len(_latency_history)
                        return result

                if response.status_code >= 400:
                    resp_body = response.text[:300]
                    logger.warning("HTTP %d for %s %s: %s", response.status_code, method, url, resp_body)
                    # 记录失败
                    _api_stats["fail_count"] += 1
                    _api_stats["last_fail_ts"] = time.time()
                    _api_stats["consecutive_failures"] += 1
                    return {"code": str(response.status_code), "msg": resp_body, "data": []}

                result = response.json()
                code = result.get("code", "0")
                if code == "0":
                    return result
                # 降级处理
                delay = _RETRY_DELAY_MAP.get(code)
                if delay and attempt < max_retries:
                    logger.warning("API降级[%s] %s %s 等待%ds 第%d次", code, method, path, delay, attempt+1)
                    time.sleep(delay)
                    continue
                if attempt < max_retries:
                    time.sleep(0.5)
                    continue
                # 最终失败
                _api_stats["fail_count"] += 1
                _api_stats["last_fail_ts"] = time.time()
                _api_stats["consecutive_failures"] += 1
                return result
            except httpx.TimeoutException:
                if attempt < max_retries:
                    logger.warning("超时重试 %s %s (%.1fs)", method, path, time.time() - t0)
                    time.sleep(1)
                    continue
                logger.warning("HTTP/2超时 %s %s (%.1fs)", method, path, time.time() - t0)
                _api_stats["fail_count"] += 1
                _api_stats["last_fail_ts"] = time.time()
                _api_stats["consecutive_failures"] += 1
                return {"code": "-1", "msg": "timeout", "data": []}
            except httpx.ConnectError as e:
                if attempt < max_retries:
                    logger.warning("连接失败重试 %s: %s", url, e)
                    time.sleep(3)
                    continue
                logger.warning("HTTP/2连接失败 %s: %s", url, e)
                _api_stats["fail_count"] += 1
                _api_stats["last_fail_ts"] = time.time()
                _api_stats["consecutive_failures"] += 1
                return {"code": "-1", "msg": f"connect error: {e}", "data": []}
            except Exception as e:
                if attempt < max_retries:
                    logger.warning("异常重试 %s: %s", url, e)
                    time.sleep(1)
                    continue
                logger.warning("HTTP/2请求异常 %s: %s", url, e)
                _api_stats["fail_count"] += 1
                _api_stats["last_fail_ts"] = time.time()
                _api_stats["consecutive_failures"] += 1
                return {"code": "-1", "msg": str(e), "data": []}
        return {"code": "-1", "msg": "max retries", "data": []}

    # ── OKX API 方法 ────────────────────────────────────

    def get_instruments(self, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        return self._request("GET", "/api/v5/public/instruments", {"instType": inst_type}).get("data", [])

    def get_instrument(self, inst_id: str) -> dict[str, Any]:
        """获取单个合约的详细信息（含 ctVal, lotSz 等）"""
        data = self._request("GET", "/api/v5/public/instruments",
                             {"instType": "SWAP", "instId": inst_id}).get("data", [])
        return data[0] if data else {}

    def get_tickers(self, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        return self._request("GET", "/api/v5/market/tickers", {"instType": inst_type}).get("data", [])

    def get_ticker(self, inst_id: str) -> dict[str, Any]:
        data = self._request("GET", "/api/v5/market/ticker", {"instId": inst_id}).get("data", [])
        return data[0] if data else {}

    def get_index_ticker(self, inst_id: str) -> dict[str, Any]:
        """指数行情（index-tickers）。指数价在 /market/index-tickers，不在 /market/ticker。
        inst_id 传合约（如 BTC-USDT-SWAP），内部去掉 -SWAP 转成指数交易对（如 BTC-USDT）。"""
        uly = inst_id.replace("-SWAP", "") if inst_id.endswith("-SWAP") else inst_id
        data = self._request("GET", "/api/v5/market/index-tickers", {"instId": uly}).get("data", [])
        return data[0] if data else {}

    def get_candles(self, inst_id: str, bar: str = "15m", limit: int = 100) -> list[list]:
        return self._request("GET", "/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)}).get("data", [])

    def get_open_interest(self, inst_id: str) -> dict:
        """持仓量 (OI)"""
        data = self._request("GET", "/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst_id}).get("data", [])
        return data[0] if data else {}

    def get_positions(self, inst_id: str = "") -> list[dict]:
        """获取持仓信息（含liqPx）"""
        params = {"instType": "SWAP"}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v5/account/positions", params).get("data", [])

    def get_account_balance(self) -> dict:
        """账户余额"""
        data = self._request("GET", "/api/v5/account/balance").get("data", [])
        return data[0] if data else {}

    def get_account_config(self) -> dict:
        """账户配置（含账户模式/持仓模式）"""
        data = self._request("GET", "/api/v5/account/config").get("data", [])
        return data[0] if data else {}

    def get_position_mode(self) -> str:
        """获取持仓模式"""
        data = self._request("GET", "/api/v5/account/position-mode").get("data", [])
        return data[0].get("posMode", "") if data else ""

    def set_position_mode(self, pos_mode: str = "long_short_mode") -> dict:
        """设置双向持仓模式"""
        return self._request("POST", "/api/v5/account/set-position-mode", {"posMode": pos_mode})

    def set_account_level(self, acct_lv: str = "2") -> dict:
        """设置账户模式: 1=现货, 2=合约, 3=跨币种保证金, 4=组合保证金"""
        return self._request("POST", "/api/v5/account/set-account-level", {"acctLv": acct_lv})

    def get_max_size(self, inst_id: str, leverage: int = 0, px: float = 0) -> dict:
        """可开张数（leverage>0 时直接按指定杠杆查询，无需先set_leverage；px>0 时传当前价，API 按实际价算更准）"""
        params: dict[str, str] = {"instId": inst_id, "tdMode": "cross"}
        if leverage and leverage > 0:
            params["leverage"] = str(leverage)
        if px and px > 0:
            params["px"] = str(px)
        data = self._request("GET", "/api/v5/account/max-size", params).get("data", [])
        return data[0] if data else {}

    def cancel_algo(self, algo_id: str, inst_id: str) -> dict:
        """撤策略单（冰山单）"""
        return self._request("POST", "/api/v5/trade/cancel-advance-algos", [{"algoId": algo_id, "instId": inst_id}])

    def cancel_all_pending(self, inst_id: str) -> dict:
        """撤销指定合约所有未成交的普通限价单（逐个撤，兼容模拟盘）"""
        pending = self.get_orders_pending(inst_id)
        cancelled = 0
        for o in pending:
            oid = o.get("ordId", "")
            if oid:
                self._request("POST", "/api/v5/trade/cancel-order",
                              {"instId": inst_id, "ordId": oid})
                cancelled += 1
        return {"code": "0", "msg": f"cancelled {cancelled}/{len(pending)}"}

    def get_order(self, inst_id: str, ord_id: str) -> dict:
        """订单详情"""
        data = self._request("GET", "/api/v5/trade/order", {"instId": inst_id, "ordId": ord_id}).get("data", [])
        return data[0] if data else {}

    def cancel_order(self, inst_id: str, ord_id: str) -> dict:
        """撤销单笔未成交订单"""
        return self._request("POST", "/api/v5/trade/cancel-order",
                             {"instId": inst_id, "ordId": ord_id})

    def get_orders_pending(self, inst_id: str = "") -> list[dict]:
        """未成交订单列表"""
        params = {}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v5/trade/orders-pending", params).get("data", [])

    def get_order_history(self, inst_id: str = "", limit: int = 100) -> list[dict]:
        """历史订单（近7天）orders-history"""
        params = {"instType": "SWAP", "limit": str(limit)}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v5/trade/orders-history", params).get("data", [])

    def batch_orders(self, orders: list[dict]) -> dict:
        """批量下单"""
        return self._request("POST", "/api/v5/trade/batch-orders", body=orders)

    def get_leverage_info(self, inst_id: str, mgn_mode: str = "cross") -> dict:
        """查询合约杠杆"""
        data = self._request("GET", "/api/v5/account/leverage-info",
                             {"instId": inst_id, "mgnMode": mgn_mode}).get("data", [])
        return data[0] if data else {}

    def set_leverage(self, inst_id: str, lever: int, mgn_mode: str = "cross") -> dict:
        """设置合约杠杆"""
        return self._request("POST", "/api/v5/account/set-leverage",
                             body={"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode})

    def get_position_tiers(self, inst_type: str = "SWAP", uly: str = "", inst_family: str = "") -> list[dict]:
        """获取持仓梯度（每个档位的最大杠杆和最大张数）"""
        params = {"instType": inst_type}
        if uly:
            params["uly"] = uly
        if inst_family:
            params["instFamily"] = inst_family
        return self._request("GET", "/api/v5/public/position-tiers", params).get("data", [])

    def place_algo_order(self, inst_id: str, side: str, pos_side: str, sz: int, px: float,
                         px_var: float = 0.01, sz_limit: int = 2, time_interval: int = 1000) -> dict:
        """冰山下单 order-algo"""
        return self._request("POST", "/api/v5/trade/order-algo", body={
            "instId": inst_id, "tdMode": "cross",
            "side": side, "posSide": pos_side,
            "sz": str(sz), "px": str(px),
            "ordType": "iceberg",
            "pxVar": str(px_var),
            "szLimit": str(sz_limit),
            "timeInterval": str(time_interval),
        })

    def get_algo_pending(self, inst_id: str = "", algo_type: str = "iceberg") -> list[dict]:
        """查询未成交冰山单"""
        params = {"algoType": algo_type}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v5/trade/orders-algo-pending", params=params).get("data", [])

    def cancel_all_algos(self, inst_id: str = "", algo_type: str = "iceberg") -> dict:
        """批量撤销所有策略单（冰山单）—— 一次API调用全撤"""
        params = {"instType": "SWAP", "ordType": algo_type}
        if inst_id:
            params["instId"] = inst_id
        return self._request("POST", "/api/v5/trade/cancel-advance-algos", body=[params])

    def close_position(self, inst_id: str, pos_side: str, mgn_mode: str = "cross") -> dict:
        """市价全平指定方向仓位"""
        return self._request("POST", "/api/v5/trade/close-position", body={
            "instId": inst_id, "posSide": pos_side, "mgnMode": mgn_mode,
        })
