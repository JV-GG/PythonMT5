"""
Signal Watcher — polls SignalTrade for BUY/SELL signals and fires trades to MT5.
Runs as a background asyncio task managed via FastAPI endpoints.
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import httpx
import MetaTrader5 as mt5  # type: ignore

from config import get_settings
from mt5_service import (
    open_trade,
    close_position,
    should_execute_trade,
    is_margin_safe,
    is_drawdown_safe,
    is_daily_profit_target_safe,
    active_trades,
    register_trade,
    MT5ConnectionError,
    MT5TradeError,
)
from schemas import TradeRequest

logger = logging.getLogger(__name__)

# Maps SignalTrade pair display names (from API response "pair" field) to MT5 symbols.
# Key   = SignalTrade "pair" value, e.g. "GBP/USD"
# Value = MT5 symbol, e.g. "GBPUSD"
PAIR_DISPLAY_TO_MT5: dict[str, str] = {
    "GBP/USD": "GBPUSD",
    "EUR/USD": "EURUSD",
    "USD/JPY": "USDJPY",
    "AUD/USD": "AUDUSD",
    "BTC/USD": "BTCUSD",
    "XAU/USD": "XAUUSD",
}

# JPY pairs trade in 0.01 increments (1 pip = 0.01); non-JPY in 0.0001 (1 pip = 0.0001).
_JPY_PAIRS = {"USDJPY", "GBPJPY", "EURJPY", "AUDJPY", "CADJPY", "CHFJPY"}


# Maximum allowed SL distance (in price units) to prevent absurd SL levels
MAX_SL_DISTANCE: dict[str, float] = {
    "XAUUSD": 30.0,    # Max $30.00 price distance on Gold (3,000 points / 300 pips)
    "BTCUSD": 2500.0,  # Max $2,500.00 price distance on BTC
    "EURUSD": 0.0080,  # Max 80 pips
    "USDJPY": 1.20,    # Max 120 pips
    "AUDUSD": 0.0080,  # Max 80 pips
    "GBPUSD": 0.0080,  # Max 80 pips
}


def _is_sane_levels(entry: float, sl: float, tp: float, direction: str, symbol: str = "") -> bool:
    """
    Returns True when SL/TP are valid absolute price levels on correct sides of entry
    and within realistic safety limits.
    BUY  → SL < entry < TP
    SELL → TP < entry < SL
    """
    if direction == "buy":
        if not (sl < entry < tp):
            return False
    else:
        if not (tp < entry < sl):
            return False

    # Detect if SL or TP is a raw pip/point number instead of an absolute price level
    # (e.g. sl=400 or sl=35 when entry=4003 or entry=1.08)
    if sl < entry * 0.5 or tp < entry * 0.5:
        return False

    # Check maximum SL distance safety threshold
    max_sl_dist = MAX_SL_DISTANCE.get(symbol, 50.0)
    sl_dist = abs(entry - sl)
    if sl_dist > max_sl_dist:
        return False

    return True


def _correct_levels_from_pips(
    entry: float, sl: float, tp: float, direction: str, symbol: str
) -> tuple[float, float]:
    """
    Detects when SignalTrade sent SL/TP as raw pips/points instead of price levels,
    converts them accurately using appropriate price unit scaling, and caps SL at safety limit.
    """
    if symbol == "BTCUSD":
        unit = 1.0        # $1.00 per point
    elif symbol == "XAUUSD":
        unit = 0.01       # $0.01 per point (e.g. 380 points = $3.80, 400 points = $4.00)
    else:
        unit = 0.01 if symbol in _JPY_PAIRS else 0.0001

    max_sl_dist = MAX_SL_DISTANCE.get(symbol, 50.0)

    # Determine SL price distance
    if sl < entry * 0.5:
        # Raw points/pips sent from SignalTrade
        sl_dist = abs(sl) * unit
    else:
        # Absolute price sent, but distance might be out of range
        sl_dist = abs(entry - sl)

    # Cap SL distance to maximum safety distance
    sl_dist = min(sl_dist, max_sl_dist)

    # Determine TP price distance
    if tp < entry * 0.5:
        tp_dist = abs(tp) * unit
    else:
        tp_dist = abs(tp - entry)

    if direction == "buy":
        corrected_sl = round(entry - sl_dist, 5)
        corrected_tp = round(entry + tp_dist, 5)
    else:
        corrected_sl = round(entry + sl_dist, 5)
        corrected_tp = round(entry - tp_dist, 5)

    logger.warning(
        f"Sanity correction applied for {symbol} | entry={entry:.5f} direction={direction} "
        f"raw_sl={sl} raw_tp={tp} → corrected_sl={corrected_sl:.5f} corrected_tp={corrected_tp:.5f} "
        f"(sl_dist={sl_dist:.2f}, tp_dist={tp_dist:.2f})"
    )
    return corrected_sl, corrected_tp

# Pair codes used in SignalTrade API URLs — e.g. "BTCUSD" → GET /api/signals/BTCUSD
SIGNALTRADE_PAIR_CODES = list(PAIR_DISPLAY_TO_MT5.values())

# Track last seen timestamp per pair display to avoid duplicate executions
_last_signals: dict[str, str] = {}

# Track the last executed signal's raw fingerprint per pair.
# Uses the RAW SL/TP from SignalTrade (before any reduction) so that the same
# signal is never executed twice, even if the entry price drifts between polls.
# A genuinely new signal (different SL/TP) will have a different fingerprint.
_last_executed_signal: dict[str, str] = {}

# Track the last execution time per pair display to enforce the cooldown period.
_last_executed_time: dict[str, float] = {}


def _is_confidence_acceptable(
    confidence: float,
    signal_data: dict[str, Any],
) -> tuple[bool, str]:
    """
    Apply SignalTrade's confidence floors.
    All sessions require a minimum of 20% confidence.

    Returns (True, reason) if signal should execute, (False, reason) if blocked.
    """
    session_info = signal_data.get("sessionInfo") or {}
    session_name = session_info.get("name", "Active session")

    confirmed_floor = 20
    potential_floor = 20

    if confidence >= confirmed_floor:
        return True, f"{session_name} — confirmed, confidence {confidence}% >= {confirmed_floor}%"
    if confidence >= potential_floor:
        return True, f"{session_name} — potential, confidence {confidence}% >= {potential_floor}%"
    return False, (
        f"{session_name} — confidence {confidence}% below floors "
        f"(confirmed >= {confirmed_floor}%, potential >= {potential_floor}%)"
    )


def get_confidence_comment(confidence: float) -> str:
    """
    Format confidence value as a percentage string comment (e.g., 20%).
    If confidence is integer-like (e.g., 20.0), return '20%'.
    """
    if confidence % 1 == 0:
        return f"{int(confidence)}%"
    return f"{confidence}%"


def _transform_signal(signal_data: dict[str, Any]) -> TradeRequest | None:
    """
    Transform a SignalTrade response into a TradeRequest for MT5.

    SignalTrade response structure:
      {
        "pair": "BTC/USD",
        "sessionInfo": { "name": str, "quality": "optimal"|"good"|"poor" },
        "aiSignal": {
          "signal": "BUY" | "SELL" | "NEUTRAL",
          "confidence": number,
          "entry": number,
          "stopLoss": number,
          "takeProfit1": number,
          "takeProfit": number,    <- TP Final (TP2)
        }
      }

    Returns None if the signal is NEUTRAL, invalid, or fails the
    session-aware confidence gate (all sessions: >= 20%).
    """
    ai = signal_data.get("aiSignal", {})
    direction: str = ai.get("signal", "NEUTRAL")
    confidence: float = ai.get("confidence", 0)

    if direction not in ("BUY", "SELL"):
        return None

    acceptable, reason = _is_confidence_acceptable(confidence, signal_data)
    if not acceptable:
        logger.info(f"Signal skipped — {reason}")
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

    # Resolve symbol mappings early — needed for the sanity check below
    settings = get_settings()
    pair_display: str = signal_data.get("pair", "")
    mt5_symbol = PAIR_DISPLAY_TO_MT5.get(pair_display)
    if not mt5_symbol:
        logger.warning(f"Unknown pair display: {pair_display}")
        return None

    # Sanity-check SL/TP: detect and auto-correct pips-vs-price confusion from SignalTrade
    if not _is_sane_levels(entry, sl, tp1_value, resolved_direction, mt5_symbol):
        logger.warning(
            f"Insane SL/TP detected for {pair_display} — SL={sl} TP={tp1_value} "
            f"direction={resolved_direction} entry={entry}. Attempting pips→price correction."
        )
        sl, tp1_value = _correct_levels_from_pips(
            entry, sl, tp1_value, resolved_direction, mt5_symbol
        )
        # If still insane after correction, abort — don't send garbage to MT5
        if not _is_sane_levels(entry, sl, tp1_value, resolved_direction, mt5_symbol):
            logger.error(
                f"Correction failed for {pair_display} — SL/TP still insane "
                f"after correction. Skipping trade."
            )
            return None

    volume = 0.01 if mt5_symbol == "EURUSD" else settings.default_volume
    if mt5_symbol == "XAUUSD":
        from datetime import timezone as dt_timezone, timedelta as dt_timedelta
        malaysia_tz = dt_timezone(dt_timedelta(hours=8))
        malaysia_now = datetime.now(malaysia_tz)
        weekday = malaysia_now.weekday()
        if weekday == 4:  # Friday
            volume = settings.xauusd_friday_volume
        elif weekday in (0, 1, 2, 3):  # Monday - Thursday
            volume = settings.xauusd_weekday_volume
        else:  # Weekend fallback
            volume = settings.xauusd_friday_volume

    # ── SL/TP reduction (spread buffer) ──────────────────────────────────
    # Pull SL and TP closer to entry by reduction_pct of the entry→level
    # distance so that the broker spread doesn't eat into profit or
    # trigger SL prematurely.
    reduction_pct = settings.tp_reduction_pct
    if reduction_pct > 0:
        # SL reduction — pull closer to entry
        sl_distance = abs(sl - entry)
        sl_reduction = sl_distance * reduction_pct
        original_sl = sl

        if resolved_direction == "buy":
            # BUY SL is below entry → move it up (closer)
            sl = round(sl + sl_reduction, 5)
        else:
            # SELL SL is above entry → move it down (closer)
            sl = round(sl - sl_reduction, 5)

        logger.info(
            f"SL reduced by {reduction_pct*100:.0f}% for spread | "
            f"{pair_display} original_sl={original_sl:.5f} → adjusted_sl={sl:.5f}"
        )

        # TP1 reduction — pull closer to entry
        tp1_distance = abs(tp1_value - entry)
        tp1_reduction = tp1_distance * reduction_pct
        original_tp1 = tp1_value

        if resolved_direction == "buy":
            tp1_value = round(tp1_value - tp1_reduction, 5)
        else:
            tp1_value = round(tp1_value + tp1_reduction, 5)

        logger.info(
            f"TP1 reduced by {reduction_pct*100:.0f}% for spread | "
            f"{pair_display} original_tp1={original_tp1:.5f} → adjusted_tp1={tp1_value:.5f}"
        )

        # TP Final reduction — pull closer to entry
        if tp_final_value is not None:
            tp_final_distance = abs(tp_final_value - entry)
            tp_final_reduction = tp_final_distance * reduction_pct
            original_tp_final = tp_final_value

            if resolved_direction == "buy":
                tp_final_value = round(tp_final_value - tp_final_reduction, 5)
            else:
                tp_final_value = round(tp_final_value + tp_final_reduction, 5)

            logger.info(
                f"TP Final reduced by {reduction_pct*100:.0f}% for spread | "
                f"{pair_display} original_tp_final={original_tp_final:.5f} → adjusted_tp_final={tp_final_value:.5f}"
            )

    return TradeRequest(
        symbol=mt5_symbol,
        volume=volume,
        order_type=resolved_direction,
        sl=sl,
        tp=tp1_value,
        tp1=tp1_value,
        tp_final=tp_final_value,
        comment=get_confidence_comment(confidence),
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
        if pair_code not in settings.allowed_symbols:
            continue
        data = await _fetch_signal(client, pair_code)
        if data is None:
            continue

        pair_display = data.get("pair", "")
        timestamp = data.get("timestamp", "")
        confidence = data.get("aiSignal", {}).get("confidence", 0)

        if timestamp == _last_signals.get(pair_display):
            continue

        # Build a fingerprint from the direction and confidence.
        # This prevents duplicate trades when entry price and SL/TP levels suggested by the API drift slightly.
        ai_raw = data.get("aiSignal", {})
        raw_direction = ai_raw.get("signal", "")
        raw_fingerprint = f"{raw_direction}|{int(confidence)}"

        # Check if the same signal fingerprint was executed recently (cooldown period of 15 minutes)
        last_exec_time = _last_executed_time.get(pair_display, 0.0)
        time_elapsed = time.time() - last_exec_time
        COOLDOWN_PERIOD = 900.0  # 15 minutes in seconds

        if raw_fingerprint == _last_executed_signal.get(pair_display) and time_elapsed < COOLDOWN_PERIOD:
            _last_signals[pair_display] = timestamp
            logger.debug(
                f"Signal skipped (same signal already executed within {COOLDOWN_PERIOD/60:.0f} mins) for {pair_display} | "
                f"fingerprint={raw_fingerprint}"
            )
            continue

        trade_req = _transform_signal(data)
        if trade_req is None:
            # If the signal went neutral or below the floor, clear the last executed fingerprint
            # so that when a new signal comes in later, it can trigger a trade.
            if raw_direction not in ("BUY", "SELL") or confidence < 20:
                _last_executed_signal[pair_display] = ""
            _last_signals[pair_display] = timestamp
            continue

        session = data.get("sessionInfo") or {}
        _last_signals[pair_display] = timestamp
        logger.info(
            f"New {trade_req.order_type.upper()} signal for {pair_display} | "
            f"session={session.get('name', 'unknown')} ({session.get('quality', 'unknown')}) | "
            f"confidence={confidence}% | sl={trade_req.sl} tp={trade_req.tp}"
        )

        ai = data.get("aiSignal", {})
        signal_entry = ai.get("entry")

        # ── Direction-flip: close profitable opposite positions ──────────
        # When signal flips (e.g. existing BUY → new SELL), close opposite
        # positions that are in profit.  Losing positions are left to float
        # and will be managed by their SL/TP.
        settings = get_settings()
        opposite = "sell" if trade_req.order_type == "buy" else "buy"
        positions = mt5.positions_get(symbol=trade_req.symbol)  # Query active positions for symbol
        if positions:
            for pos in positions:
                if pos.magic != settings.magic_number:
                    continue
                pos_dir = "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell"
                if pos_dir != opposite:
                    continue
                # pos.profit includes swap + commission
                if pos.profit > 0:
                    logger.info(
                        f"Direction flip: closing profitable {pos_dir.upper()} "
                        f"position {pos.ticket} | profit={pos.profit:.2f}"
                    )
                    close_position(pos.ticket, pos.symbol, pos.volume, pos_dir)
                else:
                    logger.info(
                        f"Direction flip: letting losing {pos_dir.upper()} "
                        f"position {pos.ticket} float | profit={pos.profit:.2f}"
                    )

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

        if not is_daily_profit_target_safe()[0]:
            logger.warning(
                f"Trade blocked (daily profit target reached) for {pair_display} | "
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
            if result.order_id is None or result.executed_price is None:
                raise MT5TradeError(
                    f"Trade request returned success but missing order_id ({result.order_id}) "
                    f"or executed_price ({result.executed_price})"
                )
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

            # Record the raw signal fingerprint and execution timestamp so the same signal won't fire again
            # unless the 15-minute cooldown period has passed.
            _last_executed_signal[pair_display] = raw_fingerprint
            _last_executed_time[pair_display] = time.time()

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
        "last_executed_signals": _last_executed_signal,
    }
