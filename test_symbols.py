"""Self-check: broker symbol names reduce to the base code, and a cent/standard account picks its own instrument. Run: python test_symbols.py"""
from types import SimpleNamespace

from config import canonical_symbol
import mt5_service as ms

for name in ["XAUUSD", "XAUUSD.sc", "XAUUSD.SC", "XAUUSDc", "XAUUSD.c", "XAUUSD-c", "XAUUSDcent", "XAUUSD.cent",
             "XAUUSDm", "XAUUSD.m", "XAUUSD-STD", "XAUUSD+", "XAUUSDPRO", "#XAUUSD", "XAU_USD", "XAU/USD",
             "GOLD", "Gold.m", "GOLDc", "GOLDcent", "GOLD.sc"]:
    assert canonical_symbol(name) == "XAUUSD", (name, canonical_symbol(name))
assert canonical_symbol("BTCUSDc") == "BTCUSD" and canonical_symbol("BTCUSDT") == "BTCUSDT"
assert canonical_symbol("EURUSD.sc") == "EURUSD" and canonical_symbol("USDJPYc") == "USDJPY"


def sym(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, trade_mode=4, visible=True, expiration_time=0, path="Forex")


def pick(names: list[str], currency: str) -> str:
    ms.mt5 = SimpleNamespace(symbols_get=lambda: [sym(n) for n in names],
                             account_info=lambda: SimpleNamespace(currency=currency))
    return ms._broker_symbols("XAUUSD")[0].name


for cent_name in ["XAUUSD.sc", "XAUUSDc", "XAUUSD.c", "XAUUSDcent"]:
    for std_name in ["XAUUSD", "XAUUSD+", "XAUUSD.m", "XAUUSD-STD"]:
        assert pick([std_name, cent_name], "USC") == cent_name, (std_name, cent_name)
        assert pick([cent_name, std_name], "USD") == std_name, (std_name, cent_name)
print("ok")
