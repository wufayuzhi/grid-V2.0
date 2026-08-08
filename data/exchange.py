"""
data/exchange.py — 数据层：从交易所读取持仓与余额（只读，不含策略决策）

新架构原则：本模块只负责"从 OKX 读取数据并写入 state"，
不修改策略状态、不做决策。决策在 engine/，计算在 formulas/。

字段来源（OKX V5 /api/v5/account/positions）：
  - long_*  来自 posSide == "long" 的持仓
  - short_* 来自 posSide == "short" 的持仓
  - bePx     盈亏平衡价（旧代码没读，这里新加）
  - margin   持仓保证金 → position_margin
  - notionalUsd 名义价值 → notional_usd
  - markPx   标记价 → position.mark_px
  - total_equity 来自 /api/v5/account/balance details[].eq (USDT/USDC)

防崩溃坑：
  - OKX 模拟盘零持仓时，数值字段返回空字符串 ""，必须用 float(p.get(f) or 0)。
"""
from __future__ import annotations
import json
import os
import logging

from raw_rest_client import RawOkxRestClient
from state import get_state
from diagnostic_logger import get_diag_logger

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
API_KEY_FILE = os.path.join(DATA_DIR, "apikey.json")

# 稳定币（余额口径）
_EQUITY_CCYS = {"USDT", "USDC"}

# ─── 全局认证客户端 ───
_auth_client: RawOkxRestClient | None = None
_auth_ready: bool = False


def _safe_float(v, default: float = 0.0) -> float:
    """防崩溃转换：空串/None/非法 → default。模拟盘零持仓返回 '' 时不炸。"""
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def load_apikey() -> dict:
    """读取API密钥。兼容新格式(simulated/live)和旧格式(平铺key)。"""
    if os.path.exists(API_KEY_FILE):
        try:
            with open(API_KEY_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"load_apikey 解析失败: {e}")
    return {}


def _get_active_keys() -> dict:
    """根据当前模拟/实盘模式，取对应API密钥。"""
    cfg = load_apikey()
    st = get_state()
    if "simulated" in cfg or "live" in cfg:
        section = "simulated" if st.simulated else "live"
        return cfg.get(section, {}) or {}
    # 旧格式：平铺
    return cfg


def build_auth_client() -> RawOkxRestClient | None:
    """构建并缓存认证客户端（从 apikey.json 读取凭证，按当前模式）。"""
    global _auth_client, _auth_ready
    cfg = _get_active_keys()
    if not (cfg.get("api_key") and cfg.get("api_secret") and cfg.get("passphrase")):
        _auth_ready = False
        return None
    try:
        st = get_state()
        _auth_client = RawOkxRestClient(timeout=st.api_timeout, simulated=st.simulated)
        _auth_client.api_key = cfg["api_key"]
        _auth_client.api_secret = cfg["api_secret"]
        _auth_client.passphrase = cfg["passphrase"]
        _auth_ready = True
        mode = "模拟盘" if st.simulated else "实盘"
        logger.info(f"data/exchange 认证客户端就绪（{mode}）")
        return _auth_client
    except Exception as e:
        logger.warning(f"data/exchange 认证初始化失败: {e}")
        _auth_ready = False
        return None


def get_auth_client() -> RawOkxRestClient | None:
    """获取认证客户端（未构建则尝试构建）。"""
    if _auth_client is None or not _auth_ready:
        build_auth_client()
    return _auth_client if _auth_ready else None


def _apply_position_to_state(p: dict, st) -> None:
    """把单条持仓数据写入 state.position。p 为 OKX positions 返回的单条记录。"""
    side = p.get("posSide", "")
    contracts = int(_safe_float(p.get("pos")))
    avg_px = _safe_float(p.get("avgPx"))
    upl = _safe_float(p.get("upl"))
    liq_px = _safe_float(p.get("liqPx"))
    be_px = _safe_float(p.get("bePx"))          # 盈亏平衡价（新加）
    margin = _safe_float(p.get("margin"))
    notional = _safe_float(p.get("notionalUsd"))
    mark_px = _safe_float(p.get("markPx"))

    pos = st.position
    if side == "long":
        pos.long_contracts = contracts
        pos.long_avg_px = avg_px
        pos.long_unrealized_pnl = upl
        if liq_px > 0:
            pos.long_liq_px = liq_px
        if be_px > 0:
            pos.long_be_px = be_px
    elif side == "short":
        pos.short_contracts = contracts
        pos.short_avg_px = avg_px
        pos.short_unrealized_pnl = upl
        if liq_px > 0:
            pos.short_liq_px = liq_px
        if be_px > 0:
            pos.short_be_px = be_px
    # 通用字段（有值才覆盖）
    if mark_px > 0:
        pos.mark_px = mark_px
    if margin > 0:
        pos.position_margin = margin
    if notional > 0:
        pos.notional_usd = notional
    # 兼容字段：组合强平价（交易所能给则给）
    if liq_px > 0:
        pos.liqPx = liq_px


def sync_positions() -> None:
    """从交易所读取持仓并写入 state。

    注意：即使 API 返回空持仓列表，也保留旧值（不清零），除非有数据。
    """
    client = get_auth_client()
    if client is None:
        logger.warning("sync_positions: 无认证客户端，跳过")
        return
    st = get_state()
    diag = get_diag_logger()
    try:
        positions = client.get_positions(st.inst_id)
        if positions:
            # 有数据：先清零旧值，再用 API 返回的数据填充（避免残留旧方向）
            st.position.long_contracts = 0
            st.position.long_avg_px = 0.0
            st.position.long_unrealized_pnl = 0.0
            st.position.short_contracts = 0
            st.position.short_avg_px = 0.0
            st.position.short_unrealized_pnl = 0.0
            for p in positions:
                _apply_position_to_state(p, st)
            # API 已返回真实强平价 → 清除估算值（不复写组合强平价参与 min 计算）
            if st.position.long_liq_px > 0 or st.position.short_liq_px > 0:
                st.position.liqPx = 0
            diag.debug("DATA",
                f"持仓同步: 多={st.position.long_contracts}张@{st.position.long_avg_px:.1f} "
                f"浮盈={st.position.long_unrealized_pnl:.2f} 强平={st.position.long_liq_px} "
                f"保本={st.position.long_be_px}, "
                f"空={st.position.short_contracts}张@{st.position.short_avg_px:.1f} "
                f"浮盈={st.position.short_unrealized_pnl:.2f} 强平={st.position.short_liq_px} "
                f"保本={st.position.short_be_px}",
                {"long": st.position.long_contracts,
                 "short": st.position.short_contracts})
        else:
            # API 返回空列表：可能是暂时 API 问题或平仓线程刚执行完，保留旧值
            diag.debug("DATA", "持仓API返回空，保留旧值")
    except Exception as e:
        logger.debug(f"sync_positions: {e}")
        diag.warn("ERROR", f"持仓数据拉取失败: {e}")


def sync_equity() -> None:
    """从交易所读取账户总权益并写入 st.total_equity（只更新 total_equity，不覆写 capital）。"""
    client = get_auth_client()
    if client is None:
        return
    st = get_state()
    diag = get_diag_logger()
    try:
        bal = client.get_account_balance()
        if not bal:
            return
        for d in bal.get("details", []) or []:
            if d.get("ccy", "") in _EQUITY_CCYS:
                eq = _safe_float(d.get("eq"))
                if eq > 0:
                    old_eq = st.total_equity
                    st.total_equity = eq
                    diag.debug("DATA",
                        f"余额更新: {old_eq if old_eq is not None else 'N/A'} → {eq:.2f} {d.get('ccy')}",
                        {"old_equity": round(old_eq, 2) if old_eq is not None else None,
                         "new_equity": round(eq, 2),
                         "ccy": d.get("ccy")})
    except Exception as e:
        logger.debug(f"sync_equity: {e}")
        diag.warn("ERROR", f"余额数据拉取失败: {e}")


def refresh_exchange_data() -> None:
    """一键同步持仓 + 余额（数据层对外主入口）。"""
    sync_positions()
    sync_equity()
