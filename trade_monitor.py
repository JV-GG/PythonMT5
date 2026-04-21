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


# ── Phase 2 triggering ────────────────────────────────────────────────────────

def _is_price_near_tp1(symbol: str, direction: str, tp1: float, proximity: float) -> bool:
    """Return True if the current market price is within `proximity` of TP1."""
    tick = _get_tick(symbol)
    if tick is None:
        return False
    price = tick["ask"] if direction == "buy" else tick["bid"]
    return abs(price - tp1) <= proximity


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
            # Initial Phase 2 SL = TP1 (locks minimum profit)
            new_sl = trade.initial_tp1
            new_tp = trade.tp2

            if _modify_position(ticket, new_sl, new_tp):
                trade.phase = PHASE_2_TRAILING
                trade.current_sl = new_sl
                trade.current_tp = new_tp
                trade.triggered_at = current_price
            return

    # ── Phase 2: trailing TP and SL ─────────────────────────────────────────
    if trade.phase == PHASE_2_TRAILING:
        tick = _get_tick(symbol)
        if tick is None:
            return
        market_price = tick["ask"] if is_buy else tick["bid"]

        new_tp = trade.current_tp
        new_sl = trade.current_sl

        if is_buy:
            if market_price > trade.current_tp:
                new_tp = market_price
            if market_price > trade.current_sl:
                new_sl = market_price
        else:  # sell
            if market_price < trade.current_tp:
                new_tp = market_price
            if market_price < trade.current_sl:
                new_sl = market_price

        if new_tp != trade.current_tp or new_sl != trade.current_sl:
            if _modify_position(ticket, new_sl, new_tp):
                logger.info(
                    f"[{ticket}] Trailing update | {direction.upper()} {symbol} "
                    f"new_sl={new_sl:.5f} new_tp={new_tp:.5f}"
                )
                trade.current_tp = new_tp
                trade.current_sl = new_sl


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
