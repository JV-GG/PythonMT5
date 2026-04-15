"""
Signal Watcher — polls SignalTrade for BUY/SELL signals and fires trades to MT5.
Runs as a background asyncio task managed via FastAPI endpoints.
"""
import asyncio
import logging
from typing import Any

import httpx

from config import get_settings
from mt5_service import open_trade, MT5ConnectionError, MT5TradeError
from schemas import TradeRequest

logger = logging.getLogger(__name__)

# Maps SignalTrade pair display names (from API response "pair" field) to MT5 symbols.
# Key   = SignalTrade "pair" value, e.g. "BTC/USD"
# Value = MT5 symbol, e.g. "BTCUSD"
PAIR_DISPLAY_TO_MT5: dict[str, str] = {
    "BTC/USD": "BTCUSD",
    "GBP/USD": "GBPUSD",
    "USD/JPY": "USDJPY",
    "XAU/USD": "XAUUSD",
}

# Pair codes used in SignalTrade API URLs — e.g. "BTCUSD" → GET /api/signals/BTCUSD
# These are the keys SignalTrade expects in the URL path.
SIGNALTRADE_PAIR_CODES = list(PAIR_DISPLAY_TO_MT5.values())

# Track last seen timestamp per pair display to avoid duplicate executions
_last_signals: dict[str, str] = {}


def _transform_signal(signal_data: dict[str, Any]) -> TradeRequest | None:
    """
    Transform a SignalTrade response into a TradeRequest for MT5.

    SignalTrade response structure:
      {
        "pair": "BTC/USD",           <- display name (key for PAIR_DISPLAY_TO_MT5)
        "aiSignal": {
          "signal": "BUY" | "SELL" | "NEUTRAL",
          "entry": number,
          "stopLoss": number,
          "takeProfit1": number,   <- used as MT5 TP
          "takeProfit": number,    <- ignored (MT5 only supports 1 TP)
        }
      }

    Returns None if the signal is NEUTRAL or invalid.
    """
    ai = signal_data.get("aiSignal", {})
    direction: str = ai.get("signal", "NEUTRAL")

    if direction not in ("BUY", "SELL"):
        return None

    entry = ai.get("entry")
    sl = ai.get("stopLoss")
    tp = ai.get("takeProfit1") or ai.get("takeProfit")

    if not all(isinstance(v, (int, float)) and v > 0 for v in [entry, sl, tp]):
        logger.warning(f"Invalid signal levels — entry={entry}, sl={sl}, tp={tp}")
        return None

    settings = get_settings()
    pair_display: str = signal_data.get("pair", "")  # e.g. "BTC/USD"
    mt5_symbol = PAIR_DISPLAY_TO_MT5.get(pair_display)
    if not mt5_symbol:
        logger.warning(f"Unknown pair display: {pair_display}")
        return None

    volume = settings.xauusd_volume if mt5_symbol == "XAUUSD" else settings.default_volume

    return TradeRequest(
        symbol=mt5_symbol,
        volume=volume,
        order_type="buy" if direction == "BUY" else "sell",
        sl=float(sl),
        tp=float(tp),
    )


async def _fetch_signal(client: httpx.AsyncClient, pair_code: str) -> dict[str, Any] | None:
    """
    Fetch the latest signal for a pair from SignalTrade.

    Args:
        client: httpx async client
        pair_code: SignalTrade URL pair code (e.g. "BTCUSD" → GET /api/signals/BTCUSD)
    """
    settings = get_settings()
    url = f"{settings.signaltrade_url}/api/signals/{pair_code}"
    try:
        response = await client.get(url, timeout=15.0)
        if response.status_code != 200:
            logger.warning(f"SignalTrade returned {response.status_code} for {pair_code}")
            return None
        return response.json()
    except httpx.RequestError as e:
        logger.error(f"Failed to reach SignalTrade at {url}: {e}")
        return None


async def _poll_and_fire(client: httpx.AsyncClient) -> None:
    """Poll all pairs once and fire trades for any new BUY/SELL signals."""
    for pair_code in SIGNALTRADE_PAIR_CODES:
        # pair_code = "BTCUSD" (used in URL), also used to look up display name
        # PAIR_DISPLAY_TO_MT5.values() = ["BTCUSD", "GBPUSD", "USDJPY"]
        # PAIR_DISPLAY_TO_MT5 keys  = ["BTC/USD", "GBP/USD", "USD/JPY"]
        data = await _fetch_signal(client, pair_code)
        if data is None:
            continue

        pair_display = data.get("pair", "")  # e.g. "BTC/USD"
        timestamp = data.get("timestamp", "")

        # Skip if already processed this signal
        if timestamp == _last_signals.get(pair_display):
            continue

        trade_req = _transform_signal(data)
        if trade_req is None:
            continue  # NEUTRAL or invalid

        _last_signals[pair_display] = timestamp
        logger.info(
            f"New {trade_req.order_type.upper()} signal for {pair_display} | "
            f"sl={trade_req.sl} tp={trade_req.tp}"
        )

        try:
            result = open_trade(trade_req)
            logger.info(f"Trade fired successfully — order_id={result.order_id}")
        except (MT5ConnectionError, MT5TradeError) as e:
            logger.error(f"Failed to fire trade for {pair_display}: {e}")


async def _watcher_loop() -> None:
    """
    Background loop that polls SignalTrade at the configured interval.
    Started by /watch/start, stopped by /watch/stop.
    """
    settings = get_settings()
    interval = settings.signaltrade_poll_interval

    logger.info(f"Signal watcher started — polling every {interval}s")

    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(interval)
            try:
                await _poll_and_fire(client)
            except Exception:
                logger.exception("Unexpected error in watcher loop")


_watcher_task: asyncio.Task | None = None


def start_watcher() -> bool:
    """Start the background watcher. Returns True if started, False if already running."""
    global _watcher_task
    if _watcher_task is not None and not _watcher_task.done():
        return False
    _watcher_task = asyncio.create_task(_watcher_loop())
    return True


def stop_watcher() -> bool:
    """Stop the background watcher. Returns True if stopped, False if was not running."""
    global _watcher_task
    if _watcher_task is None or _watcher_task.done():
        return False
    _watcher_task.cancel()
    _watcher_task = None
    return True


def watcher_status() -> dict:
    """Return the current watcher status."""
    running = _watcher_task is not None and not _watcher_task.done()
    return {
        "running": running,
        "tracked_pairs": list(PAIR_DISPLAY_TO_MT5.keys()),
        "last_signals": _last_signals,
    }
