"""
engine/sync.py — API密钥管理 + 认证客户端构建（新架构 backend_v2）

职责：
  - API 密钥持久化（load_apikey / _get_active_keys / save_apikey）
  - 构建 RawOkxRestClient 认证客户端（build_auth_client）
  - 刷新可用合约列表（refresh_available_instruments）
  - 访问器（is_auth_ready / get_auth_client / get_available_inst_ids）

依赖方向：
  - 只依赖 raw_rest_client.SDK + state + diagnostic_logger
  - 行情读走 data.ticker，持仓读走 data.exchange（本模块不直接读行情/持仓）
"""
from __future__ import annotations

import json
import os
import logging
from typing import Optional, Set

from raw_rest_client import RawOkxRestClient
from state import get_state
from diagnostic_logger import get_diag_logger

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
API_KEY_FILE = os.path.join(DATA_DIR, "apikey.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ─── 全局变量 ───
_auth_client: Optional[RawOkxRestClient] = None
_auth_ready: bool = False
_available_inst_ids: Set[str] = set()


# ─── API密钥管理 ───
def load_apikey() -> dict:
    """读取API密钥。兼容新格式（simulated/live 分section）和旧格式（平铺key）"""
    if os.path.exists(API_KEY_FILE):
        try:
            with open(API_KEY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _get_active_keys() -> dict:
    """根据当前模拟/实盘模式，返回对应的API密钥section"""
    cfg = load_apikey()
    st = get_state()
    # 新格式: {"simulated": {...}, "live": {...}}
    if "simulated" in cfg or "live" in cfg:
        section = "simulated" if st.simulated else "live"
        return cfg.get(section, {})
    # 旧格式: {"api_key": "...", "api_secret": "...", "passphrase": "..."} — 兼容
    return cfg


def save_apikey(data: dict):
    """原子写入API密钥（先写tmp再replace，避免半写损坏）"""
    try:
        os.makedirs(os.path.dirname(API_KEY_FILE), exist_ok=True)
        tmp = API_KEY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, API_KEY_FILE)
    except Exception as e:
        logger.error(f"save_apikey 写入失败: {e}")


# ─── 认证客户端构建 ───
def build_auth_client():
    """按当前模式构建 RawOkxRestClient 认证客户端，并校验凭证可用性。

    凭证齐全则设置 api_key/api_secret/passphrase 并把 _auth_ready 置 True，
    否则 _auth_ready 置 False。所有API走SDK，不裸写HTTP。
    """
    global _auth_client, _auth_ready
    cfg = _get_active_keys()
    if cfg.get("api_key") and cfg.get("api_secret") and cfg.get("passphrase"):
        try:
            st = get_state()
            _auth_client = RawOkxRestClient(timeout=st.api_timeout, simulated=st.simulated)
            _auth_client.api_key = cfg["api_key"]
            _auth_client.api_secret = cfg["api_secret"]
            _auth_client.passphrase = cfg["passphrase"]
            _auth_ready = True
            mode = "模拟盘" if st.simulated else "实盘"
            logger.info(f"认证客户端就绪（{mode}）")
            try:
                bal = _auth_client.get_account_balance()
                if bal:
                    logger.info(f"API认证验证成功，账户权益: {bal.get('eq', '?')}")
                else:
                    logger.warning("API认证验证：余额返回为空")
            except Exception as e:
                logger.warning(f"API认证验证请求异常: {e}")
        except Exception as e:
            logger.warning(f"认证初始化失败: {e}")
            _auth_ready = False
    else:
        _auth_ready = False


def refresh_available_instruments():
    """刷新当前模式可交易的合约列表（模拟盘自动带 x-simulated-trading 头）"""
    global _available_inst_ids
    st = get_state()
    try:
        pub = RawOkxRestClient(timeout=10, simulated=st.simulated)
        instruments = pub.get_instruments("SWAP")
        _available_inst_ids = {i["instId"] for i in instruments} if instruments else set()
        mode = "模拟盘" if st.simulated else "实盘"
        logger.info(f"可用合约刷新: {len(_available_inst_ids)} 个（{mode}）")
    except Exception as e:
        logger.warning(f"可用合约刷新失败: {e}")


# ─── 访问器 ───
def is_auth_ready() -> bool:
    """认证客户端是否就绪"""
    return _auth_ready


def get_auth_client() -> Optional[RawOkxRestClient]:
    """获取认证客户端"""
    return _auth_client


def get_available_inst_ids() -> Set[str]:
    """获取可用合约ID集合"""
    return _available_inst_ids
