"""Pair friendly-name + bundled-logo lookup for the bot UI (logos in assets/pairs)."""
from __future__ import annotations

import os
import sys

# Friendly names mirror the client portal (app/dashboard getDisplayName), extended.
_NAMES = {
    "XAUUSD": "Gold", "XAGUSD": "Silver", "WTIUSD": "Oil", "BRENTUSD": "Brent", "NATGASUSD": "Nat Gas",
    "BTCUSD": "Bitcoin", "ETHUSD": "Ethereum", "BNBUSD": "BNB", "XRPUSD": "XRP", "SOLUSD": "Solana",
    "EURUSD": "Euro", "GBPUSD": "Pound", "USDJPY": "Yen", "AUDUSD": "Aussie", "USDCAD": "Loonie",
    "USDCHF": "Swissy", "NZDUSD": "Kiwi",
    "US500": "S&P 500", "US100": "Nasdaq", "US30": "Dow", "UK100": "FTSE", "DE40": "DAX", "JP225": "Nikkei",
}


def _assets_dir() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "pairs")


def display_name(symbol: str) -> str:
    return _NAMES.get(symbol.upper(), symbol)


def logo_path(symbol: str) -> str | None:
    """Absolute path to the bundled logo png for a symbol, or None when none is bundled."""
    s = symbol.upper()
    cands = [s.lower()]  # eurusd, usdjpy, us500, wti...
    if len(s) >= 6 and s.endswith("USD"):
        cands.append(s[:-3].lower())  # xau, btc, eth, hype, sol, wti (metals/crypto/oil base)
    d = _assets_dir()
    for c in cands:
        p = os.path.join(d, c + ".png")
        if os.path.exists(p):
            return p
    return None


if __name__ == "__main__":  # self-check
    assert display_name("XAUUSD") == "Gold"
    assert display_name("FOOBAR") == "FOOBAR"
    for sym, want in [("XAUUSD", "xau.png"), ("BTCUSD", "btc.png"),
                      ("EURUSD", "eurusd.png"), ("US500", "us500.png"), ("WTIUSD", "wti.png")]:
        p = logo_path(sym)
        assert p is None or p.endswith(want), (sym, p)
    assert logo_path("AAPL") is None  # no stock logo bundled
    print("ok")
