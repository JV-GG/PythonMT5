"""
Trade Monitor — watches open MT5 positions and applies adaptive TP/SL trailing.

Phase 1: Normal trade with initial SL and TP1.
Phase 2 (Triggered when price approaches TP1):
  - TP moves to TP2 (if set), then continues trailing upward (BUY) or downward (SELL).
  - SL moves upward only (BUY) or downward only (SELL), locking in profit.
  - Once triggered, SL and TP both follow price — never reverse.

The monitor reads open positions directly from MT5 terminal each poll cycle,
so it works for any trade — whether opened via the watcher or the /trade endpoint.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import MetaTrader5 as mt5

from config import get_settings
from schemas import TradeInfo

logger = logging.getLogger(__name__)


# ── Phase constants ────────────────────────────────────────────────────────────

PHASE_1 = "phase1"
PHASE_2_TRAILING = "phase2_trailing"


# ── In-memory trade registry ───────────────────────────────────────────────────
# Key = order_id. Value = TradeInfo.
# This registry is used to track extra metadata (tp2, phase, etc.) per trade.
_trades: dict[int, TradeInfo] = {}


# ── MT5 helpers ────────────────────────────────────────────────────────────────

def _get_open_positions() -> list[dict[str, Any]]:
    """Fetch all open positions from MT5."""
    if not mt5.terminal_info():
        return []
    positions = mt5.positions_get()
    if positions is None:
        return []
    result = []
    for p in positions:
        result.append({
            "ticket":         p.ticket,
            "symbol":         p.symbol,
            "type":           "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
            "volume":         p.volume,
            "price_open":     p.price_open,
            "price_current":  p.price_current,
            "sl":             p.sl,
            "tp":             p.tp,
            "profit":         p.profit,
            "comment":        p.comment or "",
            "magic":          p.magic,
        })
    return result


def _get_tick(symbol: str) -> dict[str, float] | None:
    """Get current bid/ask for a symbol. Returns None on failure."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return {"bid": tick.bid, "ask": tick.ask}


# ── Modification ────────────────────────────────────────────────────────────────

def _modify_position(
    ticket: int,
    new_sl: float,
    new_tp: float,
) -> bool:
    """
    Send a position modification request to MT5.
    Returns True on success, False on failure.
    """
    request = {
        "action":     mt5.TRADE_ACTION_SLTP,
        "position":   ticket,
        "sl":         new_sl,
        "tp":         new_tp,
        "magic":      get_settings().magic_number,
        "type_time":  mt5.ORDER_TIME_GTC,
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


# ── Core trailing logic ────────────────────────────────────────────────────────

def _is_price_near_tp1(symbol: str, direction: str, tp1: float, proximity: float) -> bool:
    """Return True if the current market price is within `proximity` of TP1."""
    tick = _get_tick(symbol)
    if tick is None:
        return False
    price = tick["ask"] if direction == "buy" else tick["bid"]
    return abs(price - tp1) <= proximity


def _trailing_step(symbol: str, direction: str) -> float:
    """
    Compute how far to step TP when in Phase 2 trailing mode.
    For now: mirror whatever the current price is away from the last TP.
    This keeps TP always just above/below current price.
    """
    tick = _get_tick(symbol)
    if tick is None:
        return 0.0
    return tick["ask"] if direction == "buy" else tick["bid"]


def _process_one_position(pos: dict[str, Any]) -> None:
    """
    Evaluate a single open position and apply adaptive TP/SL if needed.
    Modifies MT5 position in-place via _modify_position.
    """
    ticket = pos["ticket"]
    symbol = pos["symbol"]
    direction = pos["type"]          # "buy" or "sell"
    current_price = pos["price_current"]

    settings = get_settings()
    is_buy = direction == "buy"

    # ── Phase 1: check if we should trigger Phase 2 ──────────────────────────
    info = _trades.get(ticket)

    if info is None:
        # Not registered — create a Phase 1 shadow record.
        # We still want to monitor it for Phase 2 triggering if tp2 is set on MT5.
        # Infer phase from current TP vs MT5 SL to guess state.
        # If TP on MT5 equals the original TP1 in the comment... we keep Phase 1.
        # For non-registered trades, skip adaptive logic unless explicitly added.
        return

    if info.phase == PHASE_1:
        if info.tp2 is None:
            # No TP2 configured — nothing to trail
            return

        if _is_price_near_tp1(symbol, direction, info.initial_tp1, settings.monitor_tp1_proximity):
            # ── Trigger Phase 2 ────────────────────────────────────────────────
            logger.info(
                f"[{ticket}] Phase 2 triggered | {direction.upper()} {symbol} "
                f"price={current_price:.5f} near TP1={info.initial_tp1:.5f}"
            )
            # Initial Phase 2 SL = TP1 (locks minimum profit)
            new_sl = info.initial_tp1
            new_tp = info.tp2 if is_buy else info.tp2

            if _modify_position(ticket, new_sl, new_tp):
                info.phase = PHASE_2_TRAILING
                info.current_sl = new_sl
                info.current_tp = new_tp
                info.triggered_at = current_price
            return

    elif info.phase == PHASE_2_TRAILING:
        # ── Phase 2: trailing TP and SL ─────────────────────────────────────
        # Current market price
        tick = _get_tick(symbol)
        if tick is None:
            return
        market_price = tick["ask"] if is_buy else tick["bid"]

        # Proposed new TP = market price
        proposed_tp = market_price
        # Proposed new SL = market price (full lock)
        proposed_sl = market_price

        new_tp = info.current_tp
        new_sl = info.current_sl

        if is_buy:
            if proposed_tp > info.current_tp:
                new_tp = proposed_tp
            if proposed_sl > info.current_sl:
                new_sl = proposed_sl
        else:  # sell
            if proposed_tp < info.current_tp:
                new_tp = proposed_tp
            if proposed_sl < info.current_sl:
                new_sl = proposed_sl

        if new_tp != info.current_tp or new_sl != info.current_sl:
            if _modify_position(ticket, new_sl, new_tp):
                logger.info(
                    f"[{ticket}] Trailing update | {direction.upper()} {symbol} "
                    f"new_sl={new_sl:.5f} new_tp={new_tp:.5f}"
                )
                info.current_tp = new_tp
                info.current_sl = new_sl
        # else: no change needed — price moved against us or hasn't moved enough


# ── Monitor loop ───────────────────────────────────────────────────────────────

async def _monitor_loop() -> None:
    """
    Background asyncio loop that polls open MT5 positions every poll_interval seconds
    and applies adaptive TP/SL trailing.
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
            "order_id":   t.order_id,
            "symbol":     t.symbol,
            "direction":  t.direction,
            "entry":      t.entry_price,
            "phase":      t.phase,
            "sl":         t.current_sl,
            "tp":         t.current_tp,
            "tp2":        t.tp2,
        }
        for t in _trades.values()
    ]
    return {
        "running": running,
        "tracked_trades": trades_list,
    }


# ── Trade registration ──────────────────────────────────────────────────────────

def register_trade(
    order_id: int,
    symbol: str,
    direction: str,
    entry_price: float,
    initial_sl: float,
    initial_tp1: float,
    tp2: float | None = None,
) -> None:
    """
    Register an open trade with the monitor so it can be trailed.
    Call this after a trade is successfully opened.
    """
    _trades[order_id] = TradeInfo(
        order_id=order_id,
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        initial_sl=initial_sl,
        initial_tp1=initial_tp1,
        tp2=tp2,
        phase=PHASE_1,
        current_sl=initial_sl,
        current_tp=initial_tp1,
    )
    logger.info(
        f"[{order_id}] Trade registered for monitoring | "
        f"{direction.upper()} {symbol} entry={entry_price} "
        f"sl={initial_sl} tp1={initial_tp1} tp2={tp2}"
    )


def unregister_trade(order_id: int) -> bool:
    """
    Remove a trade from the monitor (e.g. when it is closed).
    Returns True if the trade was removed, False if it wasn't tracked.
    """
    return _trades.pop(order_id, None) is not None


def _sync_closed_positions(open_tickets: set[int]) -> None:
    """
    Remove any registered trades that are no longer open in MT5.
    Call this at the end of each monitor cycle.
    """
    stale = [tid for tid in _trades if tid not in open_tickets]
    for tid in stale:
        _trades.pop(tid, None)
        logger.info(f"[{tid}] Removed from monitor — position no longer open")
