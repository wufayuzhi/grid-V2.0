"""
OKX SDK 薄封装。所有 API 调用统一走这里。
替换原有的 raw_rest_client.py + trade_client.py。
"""
from okx.Account import AccountAPI
from okx.Trade import TradeAPI
from okx.MarketData import MarketAPI
from okx.PublicData import PublicAPI


class OkxClient:
    def __init__(self, api_key: str, secret: str, passphrase: str, simulated: bool = False):
        flag = '1' if simulated else '0'
        self._api_key = api_key
        self._secret = secret
        self._passphrase = passphrase
        self._simulated = flag == '1'
        self.account = AccountAPI(api_key, secret, passphrase, flag=flag)
        self.trade   = TradeAPI(api_key, secret, passphrase, flag=flag)
        self.market  = MarketAPI(api_key, secret, passphrase, flag=flag)
        self.public  = PublicAPI(flag=flag)

    # ═══ 持仓 (AccountAPI) ═══

    def get_positions(self, inst_id: str = "") -> list:
        result = self.account.get_positions(instType="SWAP", instId=inst_id)
        return result.get("data", []) if result else []

    def get_position_risk(self) -> list:
        result = self.account.get_position_risk(instType="SWAP")
        return result.get("data", []) if result else []

    # ═══ 账户 (AccountAPI) ═══

    def get_balance(self, ccy: str = "") -> dict:
        result = self.account.get_account_balance(ccy=ccy)
        data = result.get("data", []) if result else []
        return data[0] if data else {}

    def get_account_config(self) -> dict:
        result = self.account.get_account_config()
        data = result.get("data", []) if result else []
        return data[0] if data else {}

    def get_position_mode(self) -> str:
        cfg = self.get_account_config()
        return cfg.get("posMode", "")

    def set_position_mode(self, pos_mode: str = "long_short_mode") -> dict:
        return self.account.set_position_mode(posMode=pos_mode)

    def set_account_level(self, acct_lv: str = "2") -> dict:
        return self.account.set_account_level(acctLv=acct_lv)

    # ═══ 杠杆 (AccountAPI) ═══

    def get_leverage(self, inst_id: str) -> dict:
        return self.account.get_leverage(instId=inst_id, mgnMode="cross")

    def set_leverage(self, inst_id: str, lever: int) -> dict:
        return self.account.set_leverage(lever=lever, mgnMode="cross", instId=inst_id)

    # ═══ 最大下单量 (AccountAPI) ═══

    def get_max_size(self, inst_id: str, lever: int = 0, px: float = 0) -> dict:
        # SDK 的 get_max_avail_size 返回账户级可开限额，无需传杠杆
        if lever > 0:
            self.account.set_leverage(lever=lever, mgnMode="cross", instId=inst_id)
        return self.account.get_max_avail_size(
            instId=inst_id, tdMode="cross", ccy="USDT"
        )

    # ═══ 下单 (TradeAPI) ═══

    def place_order(self, inst_id: str, side: str, pos_side: str,
                    sz: str, px: str = "", ord_type: str = "limit") -> dict:
        return self.trade.place_order(
            instId=inst_id, tdMode="cross",
            side=side, posSide=pos_side,
            sz=sz, px=px, ordType=ord_type,
        )

    def batch_orders(self, orders: list) -> dict:
        return self.trade.place_multiple_orders(orders)

    # ═══ 撤单 (TradeAPI) ═══

    def cancel_order(self, inst_id: str, ord_id: str) -> dict:
        return self.trade.cancel_order(instId=inst_id, ordId=ord_id)

    def get_pending_orders(self, inst_id: str = "") -> list:
        result = self.trade.get_order_list(instId=inst_id)
        return result.get("data", []) if result else []

    def cancel_all_pending(self, inst_id: str) -> dict:
        """逐个撤单（兼容模拟盘不支持 cancel-batch-orders）"""
        pending = self.get_pending_orders(inst_id)
        cancelled = 0
        for o in pending:
            oid = o.get("ordId", "")
            if oid:
                self.cancel_order(inst_id, oid)
                cancelled += 1
        return {"cancelled": cancelled}

    # ═══ 平仓 (TradeAPI) ═══

    def close_position(self, inst_id: str, pos_side: str) -> dict:
        return self.trade.close_positions(
            instId=inst_id, mgnMode="cross", posSide=pos_side
        )

    # ═══ 行情 (MarketAPI) ═══
    # 注意：行情接口在 MarketAPI（需要API Key），公开数据在 PublicAPI

    def get_ticker(self, inst_id: str) -> dict:
        result = self.market.get_ticker(instId=inst_id)
        data = result.get("data", []) if result else []
        return data[0] if data else {}

    def get_tickers(self, inst_type: str = "SWAP") -> list:
        result = self.market.get_tickers(instType=inst_type)
        return result.get("data", []) if result else []

    def get_candles(self, inst_id: str, bar: str = "15m", limit: int = 100) -> list:
        result = self.market.get_candlesticks(instId=inst_id, bar=bar, limit=str(limit))
        return result.get("data", []) if result else []

    # ═══ 公开行情 (PublicAPI) ═══

    def get_instruments(self, inst_type: str = "SWAP") -> list:
        result = self.public.get_instruments(instType=inst_type)
        return result.get("data", []) if result else []

    def get_open_interest(self, inst_id: str) -> dict:
        result = self.public.get_open_interest(instType="SWAP", instId=inst_id)
        data = result.get("data", []) if result else []
        return data[0] if data else {}
