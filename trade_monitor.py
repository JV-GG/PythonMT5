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

import MetaTrader5 as mt5  # type: ignore

from config import get_settings
from schemas import PHASE_INITIAL, PHASE_PARTIAL_LOCK, PHASE_TP1_HIT, TradeInfo
from mt5_service import active_trades, modify_position_sl_tp, unregister_trade, save_active_trades

logger = logging.getLogger(__name__)


# ── MT5 helpers ──────────────────────────────────────────────────────────────

def _get_open_positions() -> list[dict]:
    """Fetch all open positions from MT5."""
    if not mt5.terminal_info():
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
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return {"bid": tick.bid, "ask": tick.ask}


# ── Stop level validations ────────────────────────────────────────────────────

def _is_valid_sl(symbol: str, direction: str, sl: float) -> bool:
    """Ensure SL is on the correct side of current price and respects stops level."""
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
        threshold = (entry + move_to_tp1 * settings.phase1_trigger_pct) if is_buy else (entry - move_to_tp1 * settings.phase1_trigger_pct)
        
        triggered = (price >= threshold) if is_buy else (price <= threshold)
        if triggered:
            locked_sl = (entry + move_to_tp1 * settings.phase1_lock_pct) if is_buy else (entry - move_to_tp1 * settings.phase1_lock_pct)
            if not _is_valid_sl(symbol, direction, locked_sl):
                logger.warning(f"[{ticket}] Partial lock SL {locked_sl:.5f} is invalid at price {price:.5f}")
                return
            
            if _modify_position(ticket, locked_sl, current_tp):
                trade.phase = PHASE_PARTIAL_LOCK
                trade.current_sl = locked_sl
                trade.current_tp = current_tp
                save_active_trades()
                logger.info(f"[{ticket}] Partial lock SL moved | locked_sl={locked_sl:.5f} price={price:.5f}")

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

            if not _is_valid_sl(symbol, direction, locked_sl):
                logger.warning(f"[{ticket}] TP1 hit SL {locked_sl:.5f} is invalid at price {price:.5f}")
                return
            if not _is_valid_tp(symbol, direction, initial_tp_final):
                logger.warning(f"[{ticket}] TP1 hit TP {initial_tp_final:.5f} is invalid at price {price:.5f}")
                return

            if _modify_position(ticket, locked_sl, initial_tp_final):
                trade.phase = PHASE_TP1_HIT
                trade.current_sl = locked_sl
                trade.current_tp = initial_tp_final
                trade.triggered_at = price
                save_active_trades()
                logger.info(f"[{ticket}] TP1 hit/approached, extending TP | locked_sl={locked_sl:.5f} tp_final={initial_tp_final:.5f} price={price:.5f}")
        else:
            # SL trails: maintain the trigger-to-lock distance (e.g. 75% - 50% = 25% of TP1 distance)
            sl_distance_pct = settings.phase1_trigger_pct - settings.phase1_lock_pct
            sl_distance = move_to_tp1 * sl_distance_pct
            new_sl = (price - sl_distance) if is_buy else (price + sl_distance)

            # Check if trailing SL has moved in profit direction
            if is_buy:
                better = (current_sl == 0.0) or (new_sl > current_sl)
            else:
                better = (current_sl == 0.0) or (new_sl < current_sl)

            if better:
                # Prevent tiny micro-adjustments
                if current_sl != 0.0 and abs(new_sl - current_sl) < 1.0 * pip_size:
                    return

                if not _is_valid_sl(symbol, direction, new_sl):
                    logger.warning(f"[{ticket}] PHASE_PARTIAL_LOCK trailing SL {new_sl:.5f} is invalid at price {price:.5f}")
                    return

                if _modify_position(ticket, new_sl, current_tp):
                    trade.current_sl = new_sl
                    save_active_trades()
                    logger.info(f"[{ticket}] PHASE_PARTIAL_LOCK trailing SL updated | new_sl={new_sl:.5f} price={price:.5f}")

    elif phase == PHASE_TP1_HIT:
        # Trail SL: trail at settings.trailing_sl_pct of entry-to-TP2 total move distance
        move_to_tp_final = abs(tp_final - entry)
        trailing_distance = move_to_tp_final * settings.trailing_sl_pct
        new_sl = (price - trailing_distance) if is_buy else (price + trailing_distance)

        # Force SL to be at least initial_tp1 once price clears it
        if is_buy:
            if new_sl < tp1 and _is_valid_sl(symbol, direction, tp1):
                new_sl = tp1
            sl_better = (current_sl == 0.0) or (new_sl > current_sl)
        else:
            if new_sl > tp1 and _is_valid_sl(symbol, direction, tp1):
                new_sl = tp1
            sl_better = (current_sl == 0.0) or (new_sl < current_sl)

        # Trail TP
        if trade.tp2 is not None:
            tp_distance = abs(trade.tp2 - tp1)
        else:
            tp_distance = abs(tp1 - entry)
        new_tp = (price + tp_distance) if is_buy else (price - tp_distance)

        if is_buy:
            tp_better = (current_tp == 0.0) or (new_tp > current_tp)
        else:
            tp_better = (current_tp == 0.0) or (new_tp < current_tp)

        if sl_better or tp_better:
            final_sl = new_sl if sl_better else current_sl
            final_tp = new_tp if tp_better else current_tp

            # Prevent tiny micro-adjustments
            sl_change = abs(final_sl - current_sl) if current_sl != 0.0 else 999.0
            tp_change = abs(final_tp - current_tp) if current_tp != 0.0 else 999.0

            if max(sl_change, tp_change) < 1.0 * pip_size:
                return

            if not _is_valid_sl(symbol, direction, final_sl):
                logger.warning(f"[{ticket}] Trailing SL {final_sl:.5f} is invalid at price {price:.5f}")
                return
            if not _is_valid_tp(symbol, direction, final_tp):
                logger.warning(f"[{ticket}] Trailing TP {final_tp:.5f} is invalid at price {price:.5f}")
                return

            if _modify_position(ticket, final_sl, final_tp):
                trade.current_sl = final_sl
                trade.current_tp = final_tp
                save_active_trades()
                logger.info(f"[{ticket}] Trailing updated | new_sl={final_sl:.5f} new_tp={final_tp:.5f} price={price:.5f}")


def _modify_position(ticket: int, new_sl: float, new_tp: float) -> bool:
    """
    Send a position modification request to MT5.
    Returns True on success, False on failure.
    """
    request = {
        "action":    mt5.TRADE_ACTION_SLTP,
        "position":  ticket,
        "sl":        new_sl,
        "tp":        new_tp,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    result = mt5.order_send(request)
    if result is None:
        logger.warning(f"order_send returned None for ticket {ticket}")
        return False
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.warning(
            f"Modify position {ticket} failed — "
            f"retcode={result.retcode} ({result.comment})"
        )
        return False
    return True


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
