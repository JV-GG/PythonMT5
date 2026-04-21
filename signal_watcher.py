"""
Signal Watcher — polls SignalTrade for BUY/SELL signals and fires trades to MT5.
Runs as a background asyncio task managed via FastAPI endpoints.
"""
import asyncio
import logging
from typing import Any

import httpx

from config import get_settings
from mt5_service import (
    open_trade,
    should_execute_trade,
    is_margin_safe,
    is_drawdown_safe,
    active_trades,
    register_trade,
    MT5ConnectionError,
    MT5TradeError,
)
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
SIGNALTRADE_PAIR_CODES = list(PAIR_DISPLAY_TO_MT5.values())

# Track last seen timestamp per pair display to avoid duplicate executions
_last_signals: dict[str, str] = {}


def _transform_signal(signal_data: dict[str, Any]) -> TradeRequest | None:
    """
    Transform a SignalTrade response into a TradeRequest for MT5.

    SignalTrade response structure:
      {
        "pair": "BTC/USD",
        "aiSignal": {
          "signal": "BUY" | "SELL" | "NEUTRAL",
          "confidence": number,
          "entry": number,
          "stopLoss": number,
          "takeProfit1": number,
          "takeProfit": number,    <- TP Final (TP2)
        }
      }

    Returns None if the signal is NEUTRAL, invalid, or confidence < 65.
    """
    ai = signal_data.get("aiSignal", {})
    direction: str = ai.get("signal", "NEUTRAL")
    confidence: float = ai.get("confidence", 0)

    if direction not in ("BUY", "SELL"):
        return None

    if confidence < 65:
        logger.info(
            f"Signal skipped — confidence {confidence}% is below 65% threshold "
            f"(potential/neutral). Direction: {direction}"
        )
        return None

    entry = ai.get("entry")
    sl = ai.get("stopLoss")
    tp1_value = ai.get("takeProfit1")
    tp_final_value = ai.get("takeProfit")

    if not all(isinstance(v, (int, float)) and v > 0 for v in [entry, sl, tp1_value]):
        logger.warning(f"Invalid signal levels — entry={entry}, sl={sl}, tp1={tp1_value}")
        return None

    resolved_direction = "buy" if direction == "BUY" else "sell"
    entry = float(entry)
    sl = float(sl)
    tp1_value = float(tp1_value)
    tp_final_value = float(tp_final_value) if tp_final_value else None

    # Directional TP validation
    if resolved_direction == "sell":
        if tp1_value >= entry:
            logger.warning(
                f"Signal rejected — SELL TP {tp1_value} is not below entry {entry}. Skipping."
            )
            return None
        if tp_final_value is not None and tp_final_value >= entry:
            logger.warning(
                f"Signal rejected — SELL TP Final {tp_final_value} is not below entry {entry}. Skipping."
            )
            return None
    else:
        if tp1_value <= entry:
            logger.warning(
                f"Signal rejected — BUY TP {tp1_value} is not above entry {entry}. Skipping."
            )
            return None
        if tp_final_value is not None and tp_final_value <= entry:
            logger.warning(
                f"Signal rejected — BUY TP Final {tp_final_value} is not above entry {entry}. Skipping."
            )
            return None

    settings = get_settings()
    pair_display: str = signal_data.get("pair", "")
    mt5_symbol = PAIR_DISPLAY_TO_MT5.get(pair_display)
    if not mt5_symbol:
        logger.warning(f"Unknown pair display: {pair_display}")
        return None

    volume = settings.xauusd_volume if mt5_symbol in ("XAUUSD", "BTCUSD") else settings.default_volume

    return TradeRequest(
        symbol=mt5_symbol,
        volume=volume,
        order_type=resolved_direction,
        sl=sl,
        tp=tp1_value,
        tp1=tp1_value,
        tp_final=tp_final_value,
    )


async def _fetch_signal(client: httpx.AsyncClient, pair_code: str) -> dict[str, Any] | None:
    """Fetch the latest signal for a pair from SignalTrade."""
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
    settings = get_settings()

    for pair_code in SIGNALTRADE_PAIR_CODES:
        data = await _fetch_signal(client, pair_code)
        if data is None:
            continue

        pair_display = data.get("pair", "")
        timestamp = data.get("timestamp", "")
        confidence = data.get("aiSignal", {}).get("confidence", 0)

        if timestamp == _last_signals.get(pair_display):
            continue

        trade_req = _transform_signal(data)
        if trade_req is None:
            continue

        _last_signals[pair_display] = timestamp
        logger.info(
            f"New {trade_req.order_type.upper()} signal for {pair_display} | "
            f"confidence={confidence}% | sl={trade_req.sl} tp={trade_req.tp}"
        )

        ai = data.get("aiSignal", {})
        signal_entry = ai.get("entry")

        # Risk gates
        if not is_margin_safe()[0]:
            logger.warning(
                f"Trade blocked (margin) for {pair_display} | "
                f"direction={trade_req.order_type}"
            )
            return

        if not is_drawdown_safe()[0]:
            logger.warning(
                f"Trade blocked (drawdown) for {pair_display} | "
                f"direction={trade_req.order_type}"
            )
            return

        # Spacing filter
        if signal_entry is not None:
            if not should_execute_trade(
                trade_req.symbol,
                trade_req.order_type,
                float(signal_entry),
                active_trades,
            ):
                logger.info(
                    f"Trade skipped (spacing) for {pair_display} | "
                    f"entry={signal_entry} direction={trade_req.order_type}"
                )
                return

        try:
            result = open_trade(trade_req)
            logger.info(f"Trade fired successfully — order_id={result.order_id}")

            register_trade(
                order_id=result.order_id,
                symbol=trade_req.symbol,
                direction=trade_req.order_type,
                entry_price=result.executed_price,
                initial_sl=trade_req.sl,
                initial_tp1=trade_req.tp1 or trade_req.tp,
                tp2=trade_req.tp_final,
            )
            logger.info(
                f"Trade registered | order_id={result.order_id} symbol={trade_req.symbol} "
                f"direction={trade_req.order_type} entry={result.executed_price} "
                f"tp1={trade_req.tp1 or trade_req.tp} tp2={trade_req.tp_final}"
            )

            # Record in SignalTrade's consecutive-direction tracker
            try:
                async with httpx.AsyncClient() as record_client:
                    await record_client.post(
                        f"{settings.signaltrade_url}/api/record-trade",
                        json={"symbol": pair_display, "direction": trade_req.order_type.upper()},
                        timeout=5.0,
                    )
                    logger.info(f"[TRACKER] Recorded {pair_display} {trade_req.order_type.upper()} in SignalTrade")
            except httpx.RequestError as rec_err:
                logger.warning(f"[TRACKER] Failed to record trade in SignalTrade: {rec_err}")

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
