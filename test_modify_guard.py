"""Self-check: SL/TP modifies are rate limited per position and the TP never moves backwards. Run: python test_modify_guard.py"""
from types import SimpleNamespace

import trade_monitor as tm

sent: list[dict] = []
fake = SimpleNamespace(
    TRADE_ACTION_SLTP=6, ORDER_TIME_GTC=0, TRADE_RETCODE_DONE=10009,
    terminal_info=lambda: True,
    symbol_info=lambda s: SimpleNamespace(trade_stops_level=0, point=0.01, digits=2),
    symbol_info_tick=lambda s: SimpleNamespace(bid=2000.0, ask=2000.2),
    order_send=lambda r: (sent.append(r), SimpleNamespace(retcode=10009, comment=""))[1],
)
tm.mt5 = fake
tm.get_settings = lambda: SimpleNamespace(dry_run=False, modify_min_interval_s=10.0)
tm.log_event = lambda *a, **k: None

# BUY, SL 1990 -> 1995 is forward; TP target 2010 is behind the current 2020, so the TP must stay
ok, sl, tp = tm._modify_position(1, "XAUUSD", "buy", 1995.0, 2010.0, 1990.0, 2020.0)
assert ok and sl == 1995.0 and tp == 2020.0, (ok, sl, tp)

# A second change inside the interval sends nothing
ok, *_ = tm._modify_position(1, "XAUUSD", "buy", 1997.0, 2030.0, 1995.0, 2020.0)
assert not ok and len(sent) == 1, sent

# Only the TP moving backwards (SELL: up) is no request at all, on a fresh ticket
ok, *_ = tm._modify_position(2, "XAUUSD", "sell", 2010.0, 1990.0, 2010.0, 1980.0)
assert not ok and len(sent) == 1, sent

# After the interval the forward move goes through
tm._last_modify_at[1] -= 11
ok, sl, tp = tm._modify_position(1, "XAUUSD", "buy", 1997.0, 2030.0, 1995.0, 2020.0)
assert ok and (sl, tp) == (1997.0, 2030.0) and len(sent) == 2, (ok, sl, tp, sent)
print("ok")
