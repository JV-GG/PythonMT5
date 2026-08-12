"""
Trade Monitor — watches open MT5 positions and applies adaptive TP/SL trailing.

Phase 1 (initial): Normal trade with initial SL and TP1. Waits for Phase 2 trigger.
Phase 2 (trailing): Triggered when price approaches TP1 by monitor_tp1_proximity.
  - TP moves to TP2 (if set), then continues trailing upward (BUY) or downward (SELL).
  - SL moves upward only (BUY) or downward only (SELL), locking in profit.
  - Once triggered, SL and TP both follow price — never reverse.

The monitor reads open positions directly from MT5 each poll cycle, so it works
for any trade — whether opened via the watcher, the /trade endpoint, or the
threaded manager in main.py.  All state lives in the shared `active_trades`
registry (mt5_service.py).

Started by main.py on app startup. Stops on app shutdown.
"""
import asyncio
import logging

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

from config import get_settings
from schemas import PHASE_INITIAL, PHASE_PARTIAL_LOCK, PHASE_TP1_HIT, TradeInfo
from mt5_service import active_trades, modify_position_sl_tp, unregister_trade, save_active_trades

logger = logging.getLogger(__name__)


# ── MT5 helpers ──────────────────────────────────────────────────────────────

def _get_open_positions() -> list[dict]:
    """Fetch all open positions from MT5."""
    settings = get_settings()
    if settings.dry_run or mt5 is None or not mt5.terminal_info():
        return []
    positions = mt5.positions_get()
    if positions is None:
        return []
    return [
        {
            "ticket":        p.ticket,
            "symbol":        p.symbol,
            "type":          "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
            "volume":        p.volume,
            "price_open":    p.price_open,
            "price_current": p.price_current,
            "sl":            p.sl,
            "tp":            p.tp,
            "profit":        p.profit,
            "comment":       p.comment or "",
            "magic":         p.magic,
        }
        for p in positions
    ]


def _get_tick(symbol: str) -> dict | None:
    """Get current bid/ask for a symbol. Returns None on failure."""
    if mt5 is None:
        return None
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return {"bid": tick.bid, "ask": tick.ask}


# ── Stop level validations & Safe SL/TP Clamping ────────────────────────────

def get_best_valid_sl(symbol: str, direction: str, target_sl: float, current_sl: float) -> float | None:
    """
    Intelligently find the best valid Stop Loss price for a position.
    
    If target_sl is valid according to MT5 trade_stops_level, returns target_sl.
    If target_sl is currently too close to market price (or on the wrong side due to rapid price fluctuations),
    clamps target_sl to the maximum safe distance allowed by MT5 right now, provided that clamped level
    is strictly better than current_sl (moving exclusively in the profit/risk-reduction direction).
    
    Returns None if no better valid SL can be set right now.
    """
    settings = get_settings()
    if settings.dry_run or mt5 is None or not mt5.terminal_info():
        return target_sl

    tick = _get_tick(symbol)
    if tick is None:
        return None
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return None

    stops_level = sym_info.trade_stops_level
    point = sym_info.point
    digits = sym_info.digits
    pip_size = _get_pip_size(symbol)

    # Spread protection: factor in live broker spread so wide spread sessions (e.g. Asian session) don't trigger Invalid Stops
    spread = abs(tick["ask"] - tick["bid"])
    min_distance = max((stops_level * point) + (spread * 1.5) + (3 * point), 5 * point)

    if direction == "buy":
        max_safe_sl = round(tick["bid"] - min_distance, digits)
        candidate_sl = min(target_sl, max_safe_sl)
        
        # Must be strictly better than current_sl
        min_improvement = 0.5 * pip_size if current_sl > 0.0 else 0.0
        if candidate_sl > (current_sl + min_improvement):
            return round(candidate_sl, digits)
        return None
    else:
        min_safe_sl = round(tick["ask"] + min_distance, digits)
        candidate_sl = max(target_sl, min_safe_sl)
        
        min_improvement = 0.5 * pip_size if current_sl > 0.0 else 0.0
        if candidate_sl < (current_sl - min_improvement) or current_sl == 0.0:
            return round(candidate_sl, digits)
        return None


def get_best_valid_tp(symbol: str, direction: str, target_tp: float, current_tp: float) -> float | None:
    """
    Intelligently find the best valid Take Profit price for a position.
    """
    if target_tp <= 0.0:
        return current_tp

    settings = get_settings()
    if settings.dry_run or mt5 is None or not mt5.terminal_info():
        return target_tp

    tick = _get_tick(symbol)
    if tick is None:
        return target_tp
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return target_tp

    stops_level = sym_info.trade_stops_level
    point = sym_info.point
    digits = sym_info.digits
    spread = abs(tick["ask"] - tick["bid"])
    min_distance = max((stops_level * point) + (spread * 1.5) + (3 * point), 5 * point)

    if direction == "buy":
        min_safe_tp = round(tick["ask"] + min_distance, digits)
        return max(target_tp, min_safe_tp)
    else:
        max_safe_tp = round(tick["bid"] - min_distance, digits)
        return min(target_tp, max_safe_tp)


def _is_valid_sl(symbol: str, direction: str, sl: float) -> bool:
    """Ensure SL is on the correct side of current price and respects stops level."""
    if mt5 is None:
        return False
    tick = _get_tick(symbol)
    if tick is None:
        return False
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return False
    stops_level = sym_info.trade_stops_level
    point = sym_info.point
    min_distance = max(stops_level, 5) * point
    
    if direction == "buy":
        return sl < tick["bid"] - min_distance
    else:
        return sl > tick["ask"] + min_distance


def _is_valid_tp(symbol: str, direction: str, tp: float) -> bool:
    """Ensure TP is on the correct side of current price and respects stops level."""
    if mt5 is None:
        return False
    tick = _get_tick(symbol)
    if tick is None:
        return False
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return False
    stops_level = sym_info.trade_stops_level
    point = sym_info.point
    min_distance = max(stops_level, 5) * point
    
    if direction == "buy":
        return tp > tick["ask"] + min_distance
    else:
        return tp < tick["bid"] - min_distance


# ── Phase 2 triggering ────────────────────────────────────────────────────────

def _is_price_near_tp1(symbol: str, direction: str, tp1: float, proximity_pips: float) -> bool:
    """Return True if the current market price is within `proximity_pips` pips of TP1."""
    tick = _get_tick(symbol)
    if tick is None:
        return False
    
    pip_size = 0.01 if "JPY" in symbol else 0.0001
    proximity_price = proximity_pips * pip_size
    
    price = tick["ask"] if direction == "buy" else tick["bid"]
    return abs(price - tp1) <= proximity_price


# ── Position processing ──────────────────────────────────────────────────────

_JPY_PAIRS = {"USDJPY", "GBPJPY", "EURJPY", "AUDJPY", "CADJPY", "CHFJPY"}


def _get_pip_size(symbol: str) -> float:
    """Return pip size for the symbol."""
    if symbol == "BTCUSD":
        return 1.0
    elif symbol == "XAUUSD":
        return 0.1
    elif symbol in _JPY_PAIRS:
        return 0.01
    else:
        return 0.0001


def _process_one_position(pos: dict) -> None:
    """
    Evaluate a single registered trade and apply Phase 2 adaptive TP/SL if needed.
    Reads/writes from the shared `active_trades` registry.
    """
    ticket = pos["ticket"]
    symbol = pos["symbol"]
    direction = pos["type"]         # "buy" or "sell"
    current_sl = pos["sl"]
    current_tp = pos["tp"]
    is_buy = direction == "buy"

    trade = active_trades.get(ticket)
    if trade is None:
        return

    tick = _get_tick(symbol)
    if tick is None:
        return
    price = tick["bid"] if is_buy else tick["ask"]

    entry = trade.entry_price
    tp1 = trade.initial_tp1
    tp_final = trade.tp2 if trade.tp2 is not None else tp1
    phase = trade.phase
    pip_size = _get_pip_size(symbol)

    settings = get_settings()

    if phase == PHASE_INITIAL:
        # BUY: price moves up toward TP1; SELL: price moves down toward TP1
        move_to_tp1 = abs(tp1 - entry)
        threshold_60 = round((entry + move_to_tp1 * settings.phase1_trigger_pct) if is_buy else (entry - move_to_tp1 * settings.phase1_trigger_pct), 6)
        
        triggered_60 = (price >= threshold_60) if is_buy else (price <= threshold_60)
        if triggered_60:
            locked_sl = (entry + move_to_tp1 * settings.phase1_lock_pct) if is_buy else (entry - move_to_tp1 * settings.phase1_lock_pct)
            
            success, applied_sl, applied_tp = _modify_position(ticket, symbol, direction, locked_sl, current_tp, current_sl, current_tp)
            if success:
                trade.phase = PHASE_PARTIAL_LOCK
                trade.current_sl = applied_sl
                trade.current_tp = applied_tp
                save_active_trades()
                logger.info(f"[{ticket}] {symbol} {direction.upper()} | Phase 1 lock SL applied (+50% TP1 distance) | applied_sl={applied_sl:.5f} (target={locked_sl:.5f}) price={price:.5f}")

        elif settings.early_risk_reduction_enabled:
            # 2. Check 50% trigger (Lock 25% TP1 profit)
            threshold_50 = round((entry + move_to_tp1 * settings.early_step_50_trigger_pct) if is_buy else (entry - move_to_tp1 * settings.early_step_50_trigger_pct), 6)
            triggered_50 = (price >= threshold_50) if is_buy else (price <= threshold_50)

            if triggered_50:
                lock_50_sl = (entry + move_to_tp1 * settings.early_step_50_lock_pct) if is_buy else (entry - move_to_tp1 * settings.early_step_50_lock_pct)
                success, applied_sl, _ = _modify_position(ticket, symbol, direction, lock_50_sl, current_tp, current_sl, current_tp)
                if success:
                    trade.current_sl = applied_sl
                    save_active_trades()
                    logger.info(f"[{ticket}] {symbol} {direction.upper()} | Early 50% TP1 move | SL locked at +{settings.early_step_50_lock_pct:.0%} profit ({applied_sl:.5f}) at price {price:.5f}")

            else:
                # 3. Check 40% trigger (Lock 10% TP1 profit)
                threshold_40 = round((entry + move_to_tp1 * settings.early_step_40_trigger_pct) if is_buy else (entry - move_to_tp1 * settings.early_step_40_trigger_pct), 6)
                triggered_40 = (price >= threshold_40) if is_buy else (price <= threshold_40)

                if triggered_40:
                    lock_40_sl = (entry + move_to_tp1 * settings.early_step_40_lock_pct) if is_buy else (entry - move_to_tp1 * settings.early_step_40_lock_pct)
                    success, applied_sl, _ = _modify_position(ticket, symbol, direction, lock_40_sl, current_tp, current_sl, current_tp)
                    if success:
                        trade.current_sl = applied_sl
                        save_active_trades()
                        logger.info(f"[{ticket}] {symbol} {direction.upper()} | Early 40% TP1 move | SL locked at +{settings.early_step_40_lock_pct:.0%} profit ({applied_sl:.5f}) at price {price:.5f}")

                else:
                    # 4. Check 30% trigger (Lock 5% TP1 profit)
                    threshold_30 = round((entry + move_to_tp1 * settings.early_step_30_trigger_pct) if is_buy else (entry - move_to_tp1 * settings.early_step_30_trigger_pct), 6)
                    triggered_30 = (price >= threshold_30) if is_buy else (price <= threshold_30)

                    if triggered_30:
                        lock_30_sl = (entry + move_to_tp1 * settings.early_step_30_lock_pct) if is_buy else (entry - move_to_tp1 * settings.early_step_30_lock_pct)
                        success, applied_sl, _ = _modify_position(ticket, symbol, direction, lock_30_sl, current_tp, current_sl, current_tp)
                        if success:
                            trade.current_sl = applied_sl
                            save_active_trades()
                            logger.info(f"[{ticket}] {symbol} {direction.upper()} | Early 30% TP1 move | SL locked at +{settings.early_step_30_lock_pct:.0%} profit ({applied_sl:.5f}) at price {price:.5f}")

                    else:
                        # 5. Check 25% trigger (Lock 2% TP1 profit)
                        threshold_25 = round((entry + move_to_tp1 * settings.early_step_25_trigger_pct) if is_buy else (entry - move_to_tp1 * settings.early_step_25_trigger_pct), 6)
                        triggered_25 = (price >= threshold_25) if is_buy else (price <= threshold_25)

                        if triggered_25:
                            lock_25_sl = (entry + move_to_tp1 * settings.early_step_25_lock_pct) if is_buy else (entry - move_to_tp1 * settings.early_step_25_lock_pct)
                            success, applied_sl, _ = _modify_position(ticket, symbol, direction, lock_25_sl, current_tp, current_sl, current_tp)
                            if success:
                                trade.current_sl = applied_sl
                                save_active_trades()
                                logger.info(f"[{ticket}] {symbol} {direction.upper()} | Early 25% TP1 move | SL locked at +{settings.early_step_25_lock_pct:.0%} profit ({applied_sl:.5f}) at price {price:.5f}")

                        else:
                            # 6. Check 20% trigger (Cut SL risk by 50%)
                            threshold_20 = round((entry + move_to_tp1 * settings.early_risk_cut_trigger_pct) if is_buy else (entry - move_to_tp1 * settings.early_risk_cut_trigger_pct), 6)
                            triggered_20 = (price >= threshold_20) if is_buy else (price <= threshold_20)

                            if triggered_20 and trade.initial_sl > 0:
                                initial_risk = abs(entry - trade.initial_sl)
                                risk_cut_sl = (entry - initial_risk * 0.50) if is_buy else (entry + initial_risk * 0.50)
                                success, applied_sl, _ = _modify_position(ticket, symbol, direction, risk_cut_sl, current_tp, current_sl, current_tp)
                                if success:
                                    trade.current_sl = applied_sl
                                    save_active_trades()
                                    logger.info(f"[{ticket}] {symbol} {direction.upper()} | Early risk reduction (20% TP1 move) | SL cut 50% closer to Entry ({applied_sl:.5f}) at price {price:.5f}")

    elif phase == PHASE_PARTIAL_LOCK:
        move_to_tp1 = abs(tp1 - entry)
        near_tp1 = _is_price_near_tp1(symbol, direction, tp1, settings.monitor_tp1_proximity)
        hit_tp1 = (price >= tp1) if is_buy else (price <= tp1)
        
        if near_tp1 or hit_tp1:
            locked_sl = (entry + move_to_tp1 * settings.phase2_lock_pct) if is_buy else (entry - move_to_tp1 * settings.phase2_lock_pct)
            
            # Determine initial extended TP for Phase 3
            if trade.tp2 is not None:
                initial_tp_final = trade.tp2
            else:
                initial_tp_final = (tp1 + move_to_tp1) if is_buy else (tp1 - move_to_tp1)

            success, applied_sl, applied_tp = _modify_position(ticket, symbol, direction, locked_sl, initial_tp_final, current_sl, current_tp)
            if success:
                trade.phase = PHASE_TP1_HIT
                trade.current_sl = applied_sl
                trade.current_tp = applied_tp
                trade.triggered_at = price
                save_active_trades()
                logger.info(f"[{ticket}] {symbol} {direction.upper()} | TP1 hit/approached, extending TP | locked_sl={applied_sl:.5f} tp_final={applied_tp:.5f} price={price:.5f}")
        else:
            # SL trails: maintain the trigger-to-lock distance (e.g. 75% - 50% = 25% of TP1 distance)
            sl_distance_pct = settings.phase1_trigger_pct - settings.phase1_lock_pct
            sl_distance = move_to_tp1 * sl_distance_pct
            new_sl = (price - sl_distance) if is_buy else (price + sl_distance)

            success, applied_sl, _ = _modify_position(ticket, symbol, direction, new_sl, current_tp, current_sl, current_tp)
            if success:
                trade.current_sl = applied_sl
                save_active_trades()
                logger.debug(f"[{ticket}] {symbol} {direction.upper()} | PHASE_PARTIAL_LOCK trailing SL updated | new_sl={applied_sl:.5f} price={price:.5f}")

    elif phase == PHASE_TP1_HIT:
        # Trail SL: trail at settings.trailing_sl_pct of entry-to-TP2 total move distance
        move_to_tp_final = abs(tp_final - entry)
        trailing_distance = move_to_tp_final * settings.trailing_sl_pct
        new_sl = (price - trailing_distance) if is_buy else (price + trailing_distance)

        # Force SL to be at least initial_tp1 once price clears it
        if is_buy:
            if new_sl < tp1:
                new_sl = tp1
        else:
            if new_sl > tp1:
                new_sl = tp1

        # Trail TP
        if trade.tp2 is not None:
            tp_distance = abs(trade.tp2 - tp1)
        else:
            tp_distance = abs(tp1 - entry)
        new_tp = (price + tp_distance) if is_buy else (price - tp_distance)

        success, applied_sl, applied_tp = _modify_position(ticket, symbol, direction, new_sl, new_tp, current_sl, current_tp)
        if success:
            trade.current_sl = applied_sl
            trade.current_tp = applied_tp
            save_active_trades()
            logger.info(f"[{ticket}] {symbol} {direction.upper()} | Trailing updated | new_sl={applied_sl:.5f} new_tp={applied_tp:.5f} price={price:.5f}")


def _modify_position(
    ticket: int,
    symbol: str,
    direction: str,
    target_sl: float,
    target_tp: float,
    current_sl: float = 0.0,
    current_tp: float = 0.0,
) -> tuple[bool, float, float]:
    """
    Send position modification to MT5 with retry resilience and SL clamping fallback.
    Returns (success: bool, applied_sl: float, applied_tp: float).
    """
    settings = get_settings()
    if settings.dry_run or mt5 is None or not mt5.terminal_info():
        return True, target_sl, target_tp

    best_sl_val = get_best_valid_sl(symbol, direction, target_sl, current_sl)
    best_sl: float = best_sl_val if best_sl_val is not None else current_sl

    best_tp_val = get_best_valid_tp(symbol, direction, target_tp, current_tp) if target_tp > 0 else current_tp
    best_tp: float = best_tp_val if best_tp_val is not None else current_tp

    # Avoid unnecessary MT5 calls if no change
    if (best_sl == current_sl or best_sl == 0.0) and (best_tp == current_tp or best_tp == 0.0):
        return False, current_sl, current_tp

    sym_info = mt5.symbol_info(symbol)
    point = sym_info.point if sym_info else 0.00001
    digits = sym_info.digits if sym_info else 5

    for attempt in range(1, 4):
        req_sl = round(best_sl, digits)
        req_tp = round(best_tp, digits) if best_tp > 0.0 else 0.0

        request = {
            "action":    mt5.TRADE_ACTION_SLTP,
            "position":  ticket,
            "sl":        req_sl,
            "tp":        req_tp,
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = mt5.order_send(request)
        if result is None:
            logger.warning(f"[{ticket}] {symbol} {direction.upper()} | order_send returned None (attempt {attempt})")
            return False, current_sl, current_tp

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            if abs(req_sl - target_sl) > (1.0 * point):
                logger.info(
                    f"[{ticket}] {symbol} {direction.upper()} | Resilient Profit Lock | Target SL {target_sl:.{digits}f} clamped to safe level {req_sl:.{digits}f} "
                    f"due to price proximity (MT5 position updated successfully)."
                )
            return True, req_sl, req_tp

        # Retcode 10016: Invalid Stops -> Retry with extra spread buffer
        if result.retcode == 10016:
            tick = _get_tick(symbol)
            m_price_str = f"bid={tick['bid']:.{digits}f}" if (tick and direction == "buy") else (f"ask={tick['ask']:.{digits}f}" if tick else "N/A")
            logger.warning(
                f"[{ticket}] {symbol} {direction.upper()} | MT5 rejected sl={req_sl:.{digits}f} at {m_price_str} (Invalid Stops 10016) | "
                f"attempt {attempt}/3 -> calculating safe fallback..."
            )
            if tick and sym_info:
                stops_level = sym_info.trade_stops_level
                pip_size = _get_pip_size(symbol)
                spread = abs(tick["ask"] - tick["bid"])
                min_distance = max((stops_level * point) + (spread * 1.5) + ((attempt * 3) * point), 5 * point)
                
                if direction == "buy":
                    # Stepping down SL away from market bid to guarantee MT5 acceptance
                    max_safe = tick["bid"] - min_distance
                    fallback_sl = round(min(req_sl - (3 * point), max_safe), digits)
                    if fallback_sl > (current_sl + (0.5 * pip_size) if current_sl > 0 else 0.0):
                        best_sl = fallback_sl
                    else:
                        break
                else:
                    # Stepping up SL away from market ask to guarantee MT5 acceptance
                    min_safe = tick["ask"] + min_distance
                    fallback_sl = round(max(req_sl + (3 * point), min_safe), digits)
                    if fallback_sl < (current_sl - (0.5 * pip_size) if current_sl > 0 else 999999.0):
                        best_sl = fallback_sl
                    else:
                        break
        else:
            logger.warning(
                f"[{ticket}] {symbol} {direction.upper()} | Modify position failed — retcode={result.retcode} ({result.comment})"
            )
            break

    return False, current_sl, current_tp


# ── Monitor loop ─────────────────────────────────────────────────────────────

async def _monitor_loop() -> None:
    """
    Background asyncio loop that polls open MT5 positions every monitor_poll_interval
    seconds and applies adaptive TP/SL trailing via the shared active_trades registry.
    """
    settings = get_settings()
    interval = settings.monitor_poll_interval
    logger.info(f"Trade monitor started — polling every {interval}s")

    while True:
        await asyncio.sleep(interval)
        try:
            positions = _get_open_positions()
            open_tickets = {pos["ticket"] for pos in positions}
            for pos in positions:
                _process_one_position(pos)
            _sync_closed_positions(open_tickets)
        except Exception:
            logger.exception("Unexpected error in monitor loop")


def _sync_closed_positions(open_tickets: set[int]) -> None:
    """
    Remove any registered trades that are no longer open in MT5.
    Call this at the end of each monitor cycle.
    """
    settings = get_settings()
    if settings.dry_run or mt5 is None or not mt5.terminal_info():
        return

    stale = [tid for tid in active_trades if tid not in open_tickets]
    for tid in stale:
        unregister_trade(tid)
        logger.info(f"[{tid}] Removed from monitor — position no longer open")


_monitor_task: asyncio.Task | None = None


def start_monitor() -> bool:
    """Start the background monitor. Returns True if started, False if already running."""
    global _monitor_task
    if _monitor_task is not None and not _monitor_task.done():
        return False
    _monitor_task = asyncio.create_task(_monitor_loop())
    return True


def stop_monitor() -> bool:
    """Stop the background monitor. Returns True if stopped, False if was not running."""
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        return False
    _monitor_task.cancel()
    _monitor_task = None
    return True


def monitor_status() -> dict:
    """Return current monitor status and all tracked trades."""
    running = _monitor_task is not None and not _monitor_task.done()
    trades_list = [
        {
            "order_id":    t.order_id,
            "symbol":      t.symbol,
            "direction":   t.direction,
            "entry":       t.entry_price,
            "phase":       t.phase,
            "initial_sl":  t.initial_sl,
            "current_sl":  t.current_sl,
            "initial_tp1": t.initial_tp1,
            "current_tp":  t.current_tp,
            "tp2":         t.tp2,
        }
        for t in active_trades.values()
    ]
    return {
        "running": running,
        "tracked_trades": trades_list,
    }
