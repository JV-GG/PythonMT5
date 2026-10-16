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
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

from config import get_settings
import mt5_service
from mt5_service import (
    log_event,
    open_trade,
    close_position,
    resolve_symbol,
    should_execute_trade,
    is_margin_safe,
    is_drawdown_safe,
    is_daily_profit_target_safe,
    is_tp_distance_safe,
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


def _is_sane_levels(entry: float, sl: float, tp: float, direction: str) -> bool:
    """
    Returns True when SL/TP are on the correct sides of entry for the given direction.
    BUY  → SL < entry < TP
    SELL → TP < entry < SL
    """
    if direction == "buy":
        return sl < entry < tp
    else:
        return tp < entry < sl


def _correct_levels_from_pips(
    entry: float, sl: float, tp: float, direction: str, symbol: str
) -> tuple[float, float]:
    """
    Detects when SignalTrade sent SL/TP as raw pips instead of price levels, computes
    the intended distance in pips, and returns corrected absolute price levels.

    Heuristic: if the raw levels are insane (SL on wrong side of entry), treat the SL
    value as a pip distance, convert to a price level, then derive TP at 1.5× risk.
    """
    if symbol == "BTCUSD":
        pip = 1.0
    elif symbol == "XAUUSD":
        pip = 0.1  # 1 Gold pip = $0.10
    else:
        pip = 0.01 if symbol in _JPY_PAIRS else 0.0001
    sl_dist_pips = round(abs(sl) / pip)
    tp_dist_pips = round(abs(tp) / pip)

    if direction == "buy":
        corrected_sl = round(entry - sl_dist_pips * pip, 5)
        corrected_tp = round(entry + max(tp_dist_pips, round(sl_dist_pips * 1.5)) * pip, 5)
    else:
        corrected_sl = round(entry + sl_dist_pips * pip, 5)
        corrected_tp = round(entry - max(tp_dist_pips, round(sl_dist_pips * 1.5)) * pip, 5)

    logger.warning(
        f"Sanity correction | symbol={symbol} entry={entry} "
        f"direction={direction} raw_sl={sl} raw_tp={tp} "
        f"→ corrected_sl={corrected_sl} corrected_tp={corrected_tp} "
        f"(detected ~{sl_dist_pips} pips / {tp_dist_pips} pips)"
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


MT5_COMMENT_MAX = 31  # MT5 rejects / truncates order comments longer than 31 characters


def get_trade_comment(confidence: float, signal_data: dict[str, Any]) -> str:
    """MT5 order comment: confidence plus the AI provider that generated the signal, e.g. '40% claude'."""
    provider = str(signal_data.get("aiProvider") or "").strip()
    provider = "".join(ch for ch in provider if ch.isalnum() or ch in "-._")   # MT5 comments: plain ASCII only
    comment = f"{get_confidence_comment(confidence)} {provider}".strip()
    return comment[:MT5_COMMENT_MAX]


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
    ai = signal_data.get("aiSignal") or {}
    direction: str = ai.get("signal") or signal_data.get("llm_signal") or "NEUTRAL"
    confidence: float = ai.get("confidence") or signal_data.get("llm_confidence") or 0

    if direction not in ("BUY", "SELL"):
        return None

    acceptable, reason = _is_confidence_acceptable(confidence, signal_data)
    if not acceptable:
        logger.info(f"Signal skipped — {reason}")
        return None

    entry = ai.get("entry") or signal_data.get("entry")
    sl = ai.get("stopLoss") or signal_data.get("stop_loss")
    tp1_value = ai.get("takeProfit1") or signal_data.get("take_profit1") or signal_data.get("takeProfit1")
    tp_final_value = ai.get("takeProfit") or signal_data.get("take_profit") or signal_data.get("takeProfit")

    pair_display: str = signal_data.get("pair") or signal_data.get("stock") or ""
    signal_data["pair"] = pair_display

    if entry is None or sl is None or tp1_value is None or not (entry > 0 and sl > 0 and tp1_value > 0):
        logger.warning(f"Invalid signal levels — entry={entry}, sl={sl}, tp1={tp1_value}")
        return None

    resolved_direction = "buy" if direction == "BUY" else "sell"
    entry_f = float(entry)
    sl_f = float(sl)
    tp1_f = float(tp1_value)
    tp_final_f = float(tp_final_value) if tp_final_value is not None else None

    entry, sl, tp1_value, tp_final_value = entry_f, sl_f, tp1_f, tp_final_f

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
    if not _is_sane_levels(entry, sl, tp1_value, resolved_direction):
        logger.warning(
            f"Insane SL/TP detected for {pair_display} — SL={sl} TP={tp1_value} "
            f"direction={resolved_direction} entry={entry}. Attempting pips→price correction."
        )
        sl, tp1_value = _correct_levels_from_pips(
            entry, sl, tp1_value, resolved_direction, mt5_symbol
        )
        # If still insane after correction, abort — don't send garbage to MT5
        if not _is_sane_levels(entry, sl, tp1_value, resolved_direction):
            logger.error(
                f"Correction failed for {pair_display} — SL/TP still insane "
                f"after correction. Skipping trade."
            )
            return None

    symbol_vol_attr = f"{mt5_symbol.lower()}_volume"
    if hasattr(settings, symbol_vol_attr):
        volume = getattr(settings, symbol_vol_attr)
    else:
        volume = settings.default_volume

    if mt5_symbol == "XAUUSD":
        from datetime import timezone as dt_timezone, timedelta as dt_timedelta
        malaysia_tz = dt_timezone(dt_timedelta(hours=8))
        malaysia_now = datetime.now(malaysia_tz)
        weekday = malaysia_now.weekday()

        weekday_names = {0: "monday", 1: "tuesday", 2: "wednesday", 3: "thursday", 4: "friday", 5: "saturday", 6: "sunday"}
        current_day_name = weekday_names.get(weekday, "").lower()

        raw_allowed = settings.xauusd_allowed_weekdays
        if isinstance(raw_allowed, str):
            allowed_list = [x.strip().lower() for x in raw_allowed.split(",") if x.strip()]
        elif isinstance(raw_allowed, list):
            allowed_list = [x.strip().lower() for x in raw_allowed]
        else:
            allowed_list = ["monday", "tuesday"]

        allowed_days = []
        for item in allowed_list:
            if item.isdigit():
                num = int(item)
                if num in weekday_names:
                    allowed_days.append(weekday_names[num])
            else:
                allowed_days.append(item)

        if current_day_name not in allowed_days:
            logger.warning(
                f"Trade blocked (XAUUSD weekday restriction): Gold trading is only allowed on {', '.join(allowed_days)} "
                f"(Current day: {current_day_name.capitalize()} in Malaysia Time)."
            )
            return None

        volume = settings.xauusd_weekday_volume

    # ── SL/TP reduction (spread buffer) ──────────────────────────────────
    # Pull SL and TP closer to entry by sl_reduction_pct / tp_reduction_pct of the
    # entry→level distance so that the broker spread doesn't eat into profit or
    # trigger SL prematurely.
    sl_reduction_pct = settings.sl_reduction_pct
    if sl_reduction_pct > 0:
        # SL reduction — pull closer to entry
        sl_distance = abs(sl - entry)
        sl_reduction = sl_distance * sl_reduction_pct
        original_sl = sl

        if resolved_direction == "buy":
            # BUY SL is below entry → move it up (closer)
            sl = round(sl + sl_reduction, 5)
        else:
            # SELL SL is above entry → move it down (closer)
            sl = round(sl - sl_reduction, 5)

        logger.debug(
            f"SL reduced by {sl_reduction_pct*100:.0f}% for spread | "
            f"{pair_display} original_sl={original_sl:.5f} → adjusted_sl={sl:.5f}"
        )

    tp_reduction_pct = settings.tp_reduction_pct
    if tp_reduction_pct > 0:
        # TP1 reduction — pull closer to entry
        tp1_distance = abs(tp1_value - entry)
        tp1_reduction = tp1_distance * tp_reduction_pct
        original_tp1 = tp1_value

        if resolved_direction == "buy":
            tp1_value = round(tp1_value - tp1_reduction, 5)
        else:
            tp1_value = round(tp1_value + tp1_reduction, 5)

        logger.debug(
            f"TP1 reduced by {tp_reduction_pct*100:.0f}% for spread | "
            f"{pair_display} original_tp1={original_tp1:.5f} → adjusted_tp1={tp1_value:.5f}"
        )

        # TP Final reduction — pull closer to entry
        if tp_final_value is not None:
            tp_final_distance = abs(tp_final_value - entry)
            tp_final_reduction = tp_final_distance * tp_reduction_pct
            original_tp_final = tp_final_value

            if resolved_direction == "buy":
                tp_final_value = round(tp_final_value - tp_final_reduction, 5)
            else:
                tp_final_value = round(tp_final_value + tp_final_reduction, 5)

            logger.debug(
                f"TP Final reduced by {tp_reduction_pct*100:.0f}% for spread | "
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
        comment=get_trade_comment(confidence, signal_data),
    )


# Feed outage state per pair: {"since": epoch, "failures": int, "reminded": epoch}
_feed_down: dict[str, dict[str, Any]] = {}
FEED_DOWN_REMINDER_S = 1800.0   # while still down, repeat the warning at most every 30 min


def _feed_failed(pair_code: str, reason: str) -> None:
    """Log a feed outage once when it starts (plus a reminder every 30 min), not on every poll."""
    now = time.time()
    state = _feed_down.get(pair_code)
    if state is None:
        _feed_down[pair_code] = {"since": now, "failures": 1, "reminded": now}
        logger.warning(f"SignalTrade unreachable for {pair_code} ({reason}) — will log again when it recovers")
        log_event("feed_down", pair=pair_code, reason=reason)
        return
    state["failures"] += 1
    if now - state["reminded"] >= FEED_DOWN_REMINDER_S:
        state["reminded"] = now
        logger.warning(
            f"SignalTrade still unreachable for {pair_code} after {(now - state['since']) / 60:.0f} min "
            f"({state['failures']} failed polls, last: {reason})"
        )
    else:
        logger.debug(f"SignalTrade unreachable for {pair_code}: {reason}")


def _feed_ok(pair_code: str) -> None:
    state = _feed_down.pop(pair_code, None)
    if state is not None:
        minutes = (time.time() - state["since"]) / 60
        logger.info(f"SignalTrade reachable again for {pair_code} after {minutes:.1f} min ({state['failures']} failed polls)")
        log_event("feed_up", pair=pair_code, down_minutes=round(minutes, 1), failed_polls=state["failures"])


async def _fetch_signal(client: httpx.AsyncClient, pair_code: str) -> dict[str, Any] | None:
    """Fetch the latest signal for a pair from SignalTrade (supports both Client & Admin LLM portals)."""
    settings = get_settings()
    headers = {"x-api-key": settings.api_key, "Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}

    # 1. Try Client Portal endpoint first (/api/signals/{pair_code})
    url = f"{settings.signaltrade_url}/api/signals/{pair_code}"
    try:
        response = await client.get(url, headers=headers, timeout=10.0)
        if response.status_code == 200:
            _feed_ok(pair_code)
            return response.json()

        # 2. Fallback to Admin LLM Portal endpoint (/api/dialogpt-signals)
        admin_url = f"{settings.signaltrade_url}/api/dialogpt-signals?pair={pair_code}&limit=1"
        admin_res = await client.get(admin_url, headers=headers, timeout=10.0)
        if admin_res.status_code == 200:
            items = admin_res.json().get("items", [])
            if items:
                _feed_ok(pair_code)
                return items[0]

        _feed_failed(pair_code, f"HTTP {response.status_code}")
        return None
    except httpx.RequestError as e:
        _feed_failed(pair_code, f"{type(e).__name__} {e}".strip())
        return None


def _signal_age_seconds(timestamp: Any, now: float) -> float | None:
    """Seconds since SignalTrade generated the signal (ISO-8601 timestamp), None if it can't be read."""
    try:
        generated = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if generated.tzinfo is None:
        return None
    return max(now - generated.timestamp(), 0.0)   # a clock a few seconds ahead still counts as fresh


def _raw_signal_levels(signal_data: dict[str, Any]) -> dict[str, float | None]:
    """Signal levels exactly as SignalTrade sent them (before the SL/TP reduction)."""
    ai = signal_data.get("aiSignal") or {}

    def num(v: Any) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "entry_raw": num(ai.get("entry") or signal_data.get("entry")),
        "sl_raw": num(ai.get("stopLoss") or signal_data.get("stop_loss")),
        "tp1_raw": num(ai.get("takeProfit1") or signal_data.get("take_profit1") or signal_data.get("takeProfit1")),
        "tp2_raw": num(ai.get("takeProfit") or signal_data.get("take_profit") or signal_data.get("takeProfit")),
    }


def _signal_outcome(signal_event: dict[str, Any], outcome: str, reason: str = "", **extra: Any) -> None:
    """Write one 'signal' event with what happened to it (executed / blocked / rejected)."""
    log_event("signal", **signal_event, outcome=outcome, reason=reason, **extra)


# Losing opposite positions already reported as "left to float" (logged once per position, not per poll)
_float_logged: set[int] = set()


async def _poll_and_fire(client: httpx.AsyncClient) -> None:
    """Poll all pairs once and fire trades for any new BUY/SELL signals."""
    settings = get_settings()

    for pair_code in SIGNALTRADE_PAIR_CODES:
        if pair_code not in settings.allowed_symbols:
            continue
        data = await _fetch_signal(client, pair_code)
        if data is None:
            continue

        pair_display = data.get("pair") or data.get("stock") or pair_code
        data["pair"] = pair_display

        timestamp = data.get("timestamp") or data.get("created_at") or data.get("id") or ""
        ai_raw = data.get("aiSignal") or {}
        raw_direction = ai_raw.get("signal") or data.get("llm_signal") or ""
        confidence = ai_raw.get("confidence") or data.get("llm_confidence") or 0

        if timestamp == _last_signals.get(pair_display):
            continue

        # ── Freshness: only act on a signal the platform generated moments ago ──
        received_at = time.time()
        signal_age = _signal_age_seconds(timestamp, received_at)
        if raw_direction in ("BUY", "SELL") and (signal_age is None or signal_age > settings.max_signal_age_s):
            _last_signals[pair_display] = timestamp
            age_text = "unknown age" if signal_age is None else f"{signal_age:.0f}s old"
            logger.warning(
                f"Signal skipped (stale) for {pair_display} | {raw_direction} {age_text} "
                f"(max {settings.max_signal_age_s:.0f}s) | timestamp={timestamp}"
            )
            log_event("signal", pair=pair_display, direction=raw_direction.lower(), confidence=confidence,
                      signal_id=str(timestamp), signal_age_s=None if signal_age is None else round(signal_age, 1),
                      outcome="blocked", reason="stale_signal")
            continue

        # Build a fingerprint from the direction and confidence.
        # This prevents duplicate trades when entry price and SL/TP levels suggested by the API drift slightly.
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
        ai = data.get("aiSignal", {})
        signal_entry = ai.get("entry")
        logger.info(
            f"New {trade_req.order_type.upper()} signal for {pair_display} | "
            f"session={session.get('name', 'unknown')} ({session.get('quality', 'unknown')}) | "
            f"confidence={confidence}% | sl={trade_req.sl} tp={trade_req.tp} tp2={trade_req.tp_final} "
            f"signal_entry={signal_entry}"
        )
        signal_event: dict[str, Any] = {
            "pair": pair_display,
            "direction": trade_req.order_type,
            "confidence": confidence,
            "session": session.get("name"),
            "session_quality": session.get("quality"),
            "signal_id": str(timestamp),
            "signal_age_s": None if signal_age is None else round(signal_age, 1),
            "ai_provider": data.get("aiProvider"),
            "ai_model": data.get("aiModel"),
            **_raw_signal_levels(data),
            "sl_sent": trade_req.sl,
            "tp1_sent": trade_req.tp1 or trade_req.tp,
            "tp2_sent": trade_req.tp_final,
        }

        # ── Direction-flip: close profitable opposite positions ──────────
        # When signal flips (e.g. existing BUY → new SELL), close opposite
        # positions that are in profit.  Losing positions are left to float
        # and will be managed by their SL/TP.
        settings = get_settings()
        opposite = "sell" if trade_req.order_type == "buy" else "buy"
        resolved_sym = resolve_symbol(trade_req.symbol)
        positions = mt5.positions_get(symbol=resolved_sym) if (mt5 and not settings.dry_run) else None
        if positions:
            for pos in positions:
                if pos.magic != settings.magic_number:
                    continue
                buy_type = getattr(mt5, "ORDER_TYPE_BUY", 0)
                pos_dir = "buy" if pos.type == buy_type else "sell"
                if pos_dir != opposite:
                    continue
                # pos.profit includes swap + commission
                if pos.profit > 0:
                    logger.info(
                        f"Direction flip: closing profitable {pos_dir.upper()} "
                        f"position {pos.ticket} | profit={pos.profit:.2f}"
                    )
                    close_position(pos.ticket, pos.symbol, pos.volume, pos_dir)
                    log_event("flip_close", order_id=pos.ticket, symbol=pos.symbol, direction=pos_dir, profit=pos.profit)
                elif pos.ticket not in _float_logged:
                    _float_logged.add(pos.ticket)
                    logger.info(
                        f"Direction flip: letting losing {pos_dir.upper()} "
                        f"position {pos.ticket} float | profit={pos.profit:.2f}"
                    )

        # ── Re-anchor levels to the broker's price ───────────────────────
        # Keep the signal's SL/TP distances but measure them from where this broker will actually fill.
        if settings.anchor_levels_to_market and signal_entry and mt5 and not settings.dry_run:
            tick = mt5.symbol_info_tick(resolved_sym)
            info = mt5.symbol_info(resolved_sym)
            if tick is None or info is None:
                logger.warning(f"Trade blocked (no tick to anchor levels) for {pair_display}")
                _signal_outcome(signal_event, "blocked", "no_tick")
                return
            market = tick.ask if trade_req.order_type == "buy" else tick.bid
            shift = market - float(signal_entry)
            sl_distance = abs(float(signal_entry) - trade_req.sl)
            signal_event["entry_gap"] = round(shift, info.digits)
            if sl_distance > 0 and abs(shift) > settings.max_entry_gap_sl_pct * sl_distance:
                logger.warning(
                    f"Trade blocked (entry gap) for {pair_display} | signal_entry={signal_entry} market={market} "
                    f"gap={shift:+.{info.digits}f} > {settings.max_entry_gap_sl_pct:.0%} of SL distance {sl_distance:.{info.digits}f}"
                )
                _signal_outcome(signal_event, "blocked", "entry_gap")
                return
            trade_req.sl = round(trade_req.sl + shift, info.digits)
            trade_req.tp = round(trade_req.tp + shift, info.digits)
            if trade_req.tp1 is not None:
                trade_req.tp1 = round(trade_req.tp1 + shift, info.digits)
            if trade_req.tp_final is not None:
                trade_req.tp_final = round(trade_req.tp_final + shift, info.digits)
            signal_entry = market
            signal_event.update(sl_sent=trade_req.sl, tp1_sent=trade_req.tp1 or trade_req.tp, tp2_sent=trade_req.tp_final)
            logger.info(
                f"Levels anchored to market for {pair_display} | gap={shift:+.{info.digits}f} | "
                f"sl={trade_req.sl} tp1={trade_req.tp1 or trade_req.tp} tp2={trade_req.tp_final}"
            )

        # Risk gates
        if not is_margin_safe()[0]:
            logger.warning(
                f"Trade blocked (margin) for {pair_display} | "
                f"direction={trade_req.order_type}"
            )
            _signal_outcome(signal_event, "blocked", "margin")
            return

        if not is_drawdown_safe()[0]:
            logger.warning(
                f"Trade blocked (drawdown) for {pair_display} | "
                f"direction={trade_req.order_type}"
            )
            _signal_outcome(signal_event, "blocked", "daily_drawdown")
            return

        if not is_daily_profit_target_safe()[0]:
            logger.warning(
                f"Trade blocked (daily profit target reached) for {pair_display} | "
                f"direction={trade_req.order_type}"
            )
            _signal_outcome(signal_event, "blocked", "daily_profit_target")
            return

        # Minimum Remaining TP Distance filter (Risk:Reward Guard)
        tp1_target = trade_req.tp1 or trade_req.tp
        if signal_entry is not None and tp1_target is not None:
            tp_ok, tp_ratio = is_tp_distance_safe(
                symbol=trade_req.symbol,
                direction=trade_req.order_type,
                signal_entry=float(signal_entry),
                signal_tp1=tp1_target,
            )
            if not tp_ok:
                logger.warning(
                    f"Trade blocked (Insufficient TP distance) for {pair_display} | "
                    f"direction={trade_req.order_type}"
                )
                _signal_outcome(signal_event, "blocked", "tp_distance", tp1_remaining_ratio=tp_ratio)
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
                _signal_outcome(signal_event, "blocked", mt5_service.last_skip_reason or "spacing")
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
                symbol=resolve_symbol(trade_req.symbol),
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
            _signal_outcome(signal_event, "executed", order_id=result.order_id, fill_price=result.executed_price,
                            bot_latency_ms=round((time.time() - received_at) * 1000))

            logger.info(
                f"Trade registered | order_id={result.order_id} symbol={trade_req.symbol} "
                f"direction={trade_req.order_type} entry={result.executed_price} "
                f"tp1={trade_req.tp1 or trade_req.tp} tp2={trade_req.tp_final}"
            )

            # Record in SignalTrade's consecutive-direction tracker
            try:
                headers = {"x-api-key": settings.api_key, "Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
                async with httpx.AsyncClient() as record_client:
                    await record_client.post(
                        f"{settings.signaltrade_url}/api/record-trade",
                        json={"symbol": pair_display, "direction": trade_req.order_type.upper()},
                        headers=headers,
                        timeout=5.0,
                    )
                    logger.info(f"[TRACKER] Recorded {pair_display} {trade_req.order_type.upper()} in SignalTrade")
            except httpx.RequestError as rec_err:
                logger.warning(f"[TRACKER] Failed to record trade in SignalTrade: {rec_err}")

        except (MT5ConnectionError, MT5TradeError) as e:
            logger.error(f"Failed to fire trade for {pair_display}: {e}")
            _signal_outcome(signal_event, "rejected", str(e)[:200])


async def _watcher_loop() -> None:
    """
    Background loop that polls SignalTrade at the configured interval.
    Started by /watch/start, stopped by /watch/stop.
    """
    settings = get_settings()
    interval = settings.signaltrade_poll_interval
    tracked_symbols = [s for s in SIGNALTRADE_PAIR_CODES if s in settings.allowed_symbols]

    logger.info(
        f"Signal watcher started — polling every {interval}s for symbols: {tracked_symbols}"
    )

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
