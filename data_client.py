"""
OKX数据客户端（纯网络层）
从screener.py抽取的数据获取部分，去掉Layer1/2/3筛选逻辑
复用raw_rest_client的 dig@1.1.1.1 DNS 绕坑方案
"""
from __future__ import annotations
import logging

from raw_rest_client import RawOkxRestClient

logger = logging.getLogger(__name__)


class OkxDataClient:
    """OKX行情数据获取 — 复用raw_rest_client的网络层"""

    def __init__(self):
        self._client = RawOkxRestClient(timeout=15)
        self._lever_map: dict[str, int] = {}
        self._ct_val_map: dict[str, float] = {}

    def fetch_instruments(self):
        """拉取全部SWAP合约的杠杆倍数和合约面值"""
        try:
            instruments = self._client.get_instruments("SWAP")
            for item in instruments:
                try:
                    inst_id = item.get("instId", "")
                    lever = int(item.get("lever", "0"))
                    if lever > 0:
                        self._lever_map[inst_id] = lever
                    ct_val = item.get("ctVal", "")
                    if ct_val and float(ct_val) > 0:
                        self._ct_val_map[inst_id] = float(ct_val)
                except (ValueError, TypeError):
                    continue
            logger.info(f"拉取 {len(self._lever_map)} 个合约杠杆/面值信息")
        except Exception as e:
            logger.warning(f"拉取instruments异常: {e}")

    def fetch_tickers_raw(self) -> list[dict]:
        """拉取全量SWAP ticker原始数据"""
        try:
            return self._client.get_tickers("SWAP")
        except Exception as e:
            logger.warning(f"拉取ticker异常: {e}")
            return []

    def fetch_ticker_raw(self, inst_id: str) -> dict | None:
        """拉取单个合约行情（省流量：~1KB vs 全量的~100KB）"""
        try:
            data = self._client.get_ticker(inst_id)
            return data if data else None  # get_ticker 已返回 dict，不是 list
        except Exception as e:
            logger.warning(f"拉取{inst_id}行情异常: {e}")
            return None

    def parse_ticker(self, raw: dict) -> dict | None:
        """解析原始ticker（弃用，用 parse_ticker_summary）"""
        try:
            return self.parse_ticker_summary(raw)
        except:
            return None

    def parse_ticker_summary(self, raw: dict) -> dict:
        """序列化为前端行情格式"""
        try:
            inst_id = raw.get("instId", "")
            last = float(raw.get("last", "0"))
            open24h = float(raw.get("open24h", "0"))
            high24h = float(raw.get("high24h", "0"))
            low24h = float(raw.get("low24h", "0"))
            bid = float(raw.get("bidPx", "0"))
            ask = float(raw.get("askPx", "0"))
            vol = float(raw.get("volCcy24h", "0"))
            change_pct = ((last - open24h) / open24h * 100) if open24h > 0 else 0
            amplitude = ((high24h - low24h) / open24h * 100) if open24h > 0 else 0
            return {
                "inst_id": inst_id,
                "last_px": last,
                "change_24h_pct": round(change_pct, 2),
                "amplitude_24h_pct": round(amplitude, 2),
                "vol_ccy_24h": vol,
                "bid_px": bid,
                "ask_px": ask,
            }
        except (ValueError, TypeError):
            return {"inst_id": raw.get("instId", ""), "last_px": 0}
