"""Checks for mt5_service.run_test_trade against a fake MT5 terminal (no broker, no network). Run: python test_test_trade.py"""
from types import SimpleNamespace as NS

import mt5_service
from config import get_settings
from mt5_service import MT5ConnectionError, MT5TradeError, TEST_MAGIC, run_test_trade


class FakeMT5:
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_TRADE_MODE_FULL = 4
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

    def __init__(self, market_closed=False, close_fails=False, netting=False, connected=True):
        self.market_closed, self.close_fails, self.netting, self.connected = market_closed, close_fails, netting, connected
        self.sent: list[dict] = []
        self.positions: dict[int, NS] = {}
        self.next_ticket = 1000
        self.sym = NS(name="XAUUSD.sc", trade_mode=4, expiration_time=0, path="Metals\\XAUUSD.sc", visible=True,
                      digits=2, point=0.01, trade_stops_level=0, filling_mode=2,
                      volume_min=0.01, volume_max=100.0, volume_step=0.01)

    def terminal_info(self):
        return NS() if self.connected else None

    def symbols_get(self):
        return (self.sym,)

    def symbol_select(self, name, enable=True):
        return True

    def symbol_info(self, name):
        return self.sym if name == self.sym.name else None

    def symbol_info_tick(self, name):
        return NS(bid=2650.00, ask=2650.30, time=0) if name == self.sym.name else None

    def account_info(self):
        return NS(currency="USC", margin_free=1e9, margin_mode=0 if self.netting else 2, login=1, server="Fake")

    def order_calc_margin(self, *a):
        return 1.0

    def last_error(self):
        return (0, "")

    def order_send(self, req):
        self.sent.append(req)
        if "position" in req:   # a close
            if self.close_fails:
                return NS(retcode=10004, comment="Requote", order=0, price=0.0, volume=0.0, deal=0)
            self.positions.pop(req["position"], None)
            return NS(retcode=10009, comment="Done", order=0, price=req["price"], volume=req["volume"], deal=0)
        if self.market_closed:
            return NS(retcode=10018, comment="Market closed", order=0, price=0.0, volume=0.0, deal=0)
        t = self.next_ticket
        self.next_ticket += 1
        self.positions[t] = NS(ticket=t, symbol=req["symbol"], volume=req["volume"], magic=req["magic"],
                               time_msc=t, price_open=req["price"])
        return NS(retcode=10009, comment="Done", order=t, price=0.0, volume=req["volume"], deal=t + 5000)

    def positions_get(self, ticket=None, symbol=None):
        if ticket is not None:
            p = self.positions.get(ticket)
            return (p,) if p else ()
        return tuple(p for p in self.positions.values() if symbol is None or p.symbol == symbol)

    def history_deals_get(self, ticket=None, position=None):
        if position is not None:
            return (NS(profit=-30.0, commission=0.0, swap=0.0, fee=0.0),)   # 30 cents of spread on a cent account
        return ()


def use(fake: FakeMT5) -> FakeMT5:
    mt5_service.mt5 = fake
    mt5_service._resolved_symbol_cache.clear()
    return fake


def main() -> None:
    s = get_settings()
    s.portal_mode, s.lot_size, s.min_lot, s.max_lot, s.dry_run = True, 0.02, 0.01, 1.0, False
    mt5_service.time.sleep = lambda _s: None   # no real waits between close retries

    # 1. Opens a BUY with dummy SL/TP and the test magic, then closes it; P/L converted from cents
    fake = use(FakeMT5())
    res = run_test_trade(["XAUUSD"])
    opened, closed = fake.sent[0], fake.sent[-1]
    assert res["closed"] is True and res["symbol"] == "XAUUSD.sc", res
    assert res["pnl"] == -0.30, res
    assert opened["magic"] == TEST_MAGIC and closed["magic"] == TEST_MAGIC, (opened, closed)
    assert opened["volume"] == 0.02, opened                        # the customer's lot, as a real signal uses
    assert opened["sl"] < 2650.00 and opened["tp"] > 2650.30, opened  # dummy levels on the right sides
    assert opened["comment"] == mt5_service.TEST_COMMENT
    assert not fake.positions, "test position must be closed"

    # 2. Market closed on every ticked pair -> a clear error, nothing left open
    fake = use(FakeMT5(market_closed=True))
    try:
        run_test_trade(["XAUUSD"]); assert False, "expected MT5TradeError"
    except MT5TradeError as e:
        assert "market is closed" in str(e), e
    assert not fake.positions

    # 3. Close keeps failing -> retried, then reported as NOT closed so the user can close it in MT5
    fake = use(FakeMT5(close_fails=True))
    res = run_test_trade(["XAUUSD"])
    assert res["closed"] is False and res["pnl"] is None, res
    assert sum(1 for r in fake.sent if "position" in r) == 3, fake.sent

    # 4. Netting account with an open position on the pair -> skipped, the real trade is untouched
    fake = use(FakeMT5(netting=True))
    fake.positions[1] = NS(ticket=1, symbol="XAUUSD.sc", volume=0.05, magic=10001, time_msc=1, price_open=2600.0)
    try:
        run_test_trade(["XAUUSD"]); assert False, "expected MT5TradeError"
    except MT5TradeError as e:
        assert "open position" in str(e), e
    assert fake.sent == [] and 1 in fake.positions

    # 5. MT5 not connected -> MT5ConnectionError, no order sent
    fake = use(FakeMT5(connected=False))
    try:
        run_test_trade(["XAUUSD"]); assert False, "expected MT5ConnectionError"
    except MT5ConnectionError:
        pass
    assert fake.sent == []

    print("test_test_trade: all 5 checks passed")


if __name__ == "__main__":
    main()
