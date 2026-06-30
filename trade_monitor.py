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

import MetaTrader5 as mt5

from config import get_settings
from schemas import PHASE_1, PHASE_2_TRAILING, TradeInfo
from mt5_service import active_trades, modify_position_sl_tp, unregister_trade

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

def _process_one_position(pos: dict) -> None:
    """
    Evaluate a single registered trade and apply Phase 2 adaptive TP/SL if needed.
    Reads/writes from the shared `active_trades` registry.
    """
    ticket = pos["ticket"]
    symbol = pos["symbol"]
    direction = pos["type"]         # "buy" or "sell"
    current_price = pos["price_current"]
    is_buy = direction == "buy"

    trade = active_trades.get(ticket)
    if trade is None:
        return

    # ── Phase 1: check if Phase 2 should trigger ──────────────────────────────
    if trade.phase == PHASE_1:
        if trade.tp2 is None:
            # No TP2 configured — Phase 2 trailing doesn't apply
            return

        settings = get_settings()
        if _is_price_near_tp1(symbol, direction, trade.initial_tp1, settings.monitor_tp1_proximity):
            # ── Trigger Phase 2 ────────────────────────────────────────────────
            logger.info(
                f"[{ticket}] Phase 2 triggered | {direction.upper()} {symbol} "
                f"price={current_price:.5f} near TP1={trade.initial_tp1:.5f}"
            )
            # Try to extend TP to TP2 to avoid closing at TP1
            new_tp = trade.tp2
            
            # Initial Phase 2 SL tries to lock in TP1 profit.
            # If TP1 is not a valid SL yet (price has not cleared it by stops level),
            # we set SL to entry_price (breakeven) if valid, or fallback to initial SL.
            if _is_valid_sl(symbol, direction, trade.initial_tp1):
                new_sl = trade.initial_tp1
            elif _is_valid_sl(symbol, direction, trade.entry_price):
                new_sl = trade.entry_price
            else:
                new_sl = trade.initial_sl

            # Verify that both are valid before modifying
            if not _is_valid_tp(symbol, direction, new_tp):
                logger.warning(f"[{ticket}] Cannot trigger Phase 2: extended TP {new_tp:.5f} is invalid.")
                return

            if _modify_position(ticket, new_sl, new_tp):
                trade.phase = PHASE_2_TRAILING
                trade.current_sl = new_sl
                trade.current_tp = new_tp
                # Record the price when Phase 2 was triggered
                tick = _get_tick(symbol)
                trade.triggered_at = (tick["bid"] if is_buy else tick["ask"]) if tick else current_price
            return

    # ── Phase 2: trailing TP and SL ─────────────────────────────────────────
    if trade.phase == PHASE_2_TRAILING:
        tick = _get_tick(symbol)
        if tick is None:
            return
        
        current_bid_ask = tick["bid"] if is_buy else tick["ask"]

        new_tp = trade.current_tp
        new_sl = trade.current_sl

        if is_buy:
            # Upgrade SL to initial_tp1 if it hasn't been set yet and is now valid
            if new_sl < trade.initial_tp1 and _is_valid_sl(symbol, direction, trade.initial_tp1):
                new_sl = trade.initial_tp1

            # Trail SL and TP upward if price moves in our direction
            if current_bid_ask > trade.triggered_at:
                delta = current_bid_ask - trade.triggered_at
                potential_sl = new_sl + delta
                potential_tp = new_tp + delta
                
                # Check if potential values are valid
                if _is_valid_sl(symbol, direction, potential_sl) and _is_valid_tp(symbol, direction, potential_tp):
                    new_sl = potential_sl
                    new_tp = potential_tp
        else:  # sell
            # Upgrade SL to initial_tp1 if it hasn't been set yet and is now valid
            if new_sl > trade.initial_tp1 and _is_valid_sl(symbol, direction, trade.initial_tp1):
                new_sl = trade.initial_tp1

            # Trail SL and TP downward if price moves in our direction
            if current_bid_ask < trade.triggered_at:
                delta = trade.triggered_at - current_bid_ask
                potential_sl = new_sl - delta
                potential_tp = new_tp - delta
                
                # Check if potential values are valid
                if _is_valid_sl(symbol, direction, potential_sl) and _is_valid_tp(symbol, direction, potential_tp):
                    new_sl = potential_sl
                    new_tp = potential_tp

        if new_tp != trade.current_tp or new_sl != trade.current_sl:
            if _modify_position(ticket, new_sl, new_tp):
                logger.info(
                    f"[{ticket}] Trailing update | {direction.upper()} {symbol} "
                    f"new_sl={new_sl:.5f} new_tp={new_tp:.5f}"
                )
                trade.current_tp = new_tp
                trade.current_sl = new_sl
                # Update the baseline if we trailed
                trade.triggered_at = current_bid_ask


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
