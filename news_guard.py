"""
News guard — protects open positions from high-impact releases (CPI, NFP, rate decisions...).

SignalTrade stops publishing new signals from 15 minutes before to 15 minutes after a high-impact release in a currency
the pair trades on, but a trade opened earlier stays open through it, and those releases often spike through the stop.
A few minutes before such a release this closes the bot's positions on the affected symbols that are in profit (banks
the gain); losing positions are left on their stop loss.

Calendar: the free Forex Factory weekly feed (same one SignalTrade uses), cached in memory and refreshed at most hourly.
If the feed is unreachable the last copy is kept.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
REFRESH_S = 3600.0
RETRY_S = 300.0

# Symbols whose news is all USD; FX pairs use both 3-letter legs
_USD_ONLY = ("XAU", "XAG", "BTC", "ETH", "US30", "US100", "US500", "NAS", "SPX", "DJ", "WTI", "BRENT", "OIL")
_SPECIAL = {"UK100": ["GBP"], "DE40": ["EUR"], "GER40": ["EUR"], "JP225": ["JPY"]}


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    country: str
    at: float  # epoch seconds
    impact: str


_events: list[CalendarEvent] = []
_next_refresh = 0.0


def symbol_currencies(symbol: str) -> list[str]:
    """Currencies whose high-impact news moves this MT5 symbol (broker suffix already stripped)."""
    s = symbol.upper()
    if s in _SPECIAL:
        return _SPECIAL[s]
    if s.startswith(_USD_ONLY):
        return ["USD"]
    if len(s) >= 6 and s[:6].isalpha():
        return [s[:3], s[3:6]]
    return []


def _parse(rows: list[dict]) -> list[CalendarEvent]:
    events = []
    for r in rows:
        try:
            at = datetime.fromisoformat(str(r["date"])).timestamp()
            events.append(CalendarEvent(str(r["title"]), str(r["country"]).upper(), at, str(r.get("impact", ""))))
        except (KeyError, ValueError, TypeError):
            continue
    return events


def refresh_due(now: float | None = None) -> bool:
    return (time.time() if now is None else now) >= _next_refresh


def cached_events() -> list[CalendarEvent]:
    return _events


def load_events(now: float | None = None) -> list[CalendarEvent]:
    """The week's events, refreshed at most hourly (every 5 minutes after a failed fetch)."""
    global _events, _next_refresh
    now = time.time() if now is None else now
    if now < _next_refresh:
        return _events
    try:
        res = httpx.get(FEED_URL, timeout=5.0, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        _events = _parse(res.json())
        _next_refresh = now + REFRESH_S
        logger.info(f"[NEWS GUARD] calendar loaded: {sum(1 for e in _events if e.impact == 'High')} high-impact events this week")
    except Exception as exc:  # network / JSON errors: keep the last copy
        _next_refresh = now + RETRY_S
        logger.warning(f"[NEWS GUARD] calendar fetch failed ({exc}); keeping {len(_events)} cached events")
    return _events


def upcoming_event(symbol: str, events: list[CalendarEvent], minutes_before: float, now: float | None = None) -> CalendarEvent | None:
    """A high-impact event for this symbol starting within the next `minutes_before` minutes, or None."""
    now = time.time() if now is None else now
    currencies = symbol_currencies(symbol)
    for e in events:
        if e.impact == "High" and e.country in currencies and 0 <= e.at - now <= minutes_before * 60:
            return e
    return None


def describe(e: CalendarEvent) -> str:
    return f"{e.country} {e.title} at {datetime.fromtimestamp(e.at, timezone.utc).strftime('%H:%M')} UTC"
