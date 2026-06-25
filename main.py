"""
FastAPI application entry point.
"""
import logging
import threading
import time
from contextlib import asynccontextmanager

import MetaTrader5 as mt5
from fastapi import FastAPI, HTTPException, status

from config import get_settings
from auth import ApiKeyAuthMiddleware
from schemas import TradeRequest, TradeResponse, ErrorResponse, TradeInfo
from mt5_service import (
    connect_mt5,
    disconnect_mt5,
    open_trade,
    modify_position_sl_tp,
    should_execute_trade,
    is_margin_safe,
    is_drawdown_safe,
    is_equity_peak_safe,
    active_trades,
    register_trade,
    unregister_trade,
    MT5ConnectionError,
    MT5TradeError,
)
from signal_watcher import start_watcher, stop_watcher, watcher_status
from trade_monitor import start_monitor, stop_monitor, monitor_status


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Thread-safe flag to stop the trade manager loop
_manager_running = True

# Minimum pip difference before updating SL (avoids spamming MT5)
SL_CHANGE_THRESHOLD = 5.0


# ── Adaptive SL/TP management ───────────────────────────────────────────────

def _get_current_price(symbol: str, direction: str) -> float | None:
    """Return current bid/ask for the given symbol based on direction."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return tick.bid if direction == "sell" else tick.ask


def _is_valid_sl(symbol: str, direction: str, sl: float) -> bool:
    """Ensure SL is on the correct side of current price (not triggering immediately)."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    current = tick.bid
    stops_level = mt5.symbol_info(symbol).trade_stops_level
    if direction == "buy":
        return sl < current - stops_level * mt5.symbol_info(symbol).point
    else:
        return sl > current + stops_level * mt5.symbol_info(symbol).point


def _manage_buy_trade(trade: TradeInfo, position) -> None:
    """
    Apply adaptive SL/TP logic for a BUY position.

    Phases:
      phase1        → partial_lock  (75% of move to TP1, lock 20% as partial profit)
      partial_lock → tp1_hit       (price reaches TP1, extend TP to TP_Final)
      tp1_hit       → trailing      (SL trails upward, TP stays at TP_Final)
    """
    symbol = trade.symbol
    entry = trade.entry_price
    tp1 = trade.initial_tp1
    tp_final = trade.tp2 if trade.tp2 is not None else tp1
    phase = trade.phase
    order_id = trade.order_id

    price = _get_current_price(symbol, "buy")
    if price is None:
        return

    move_to_tp1 = tp1 - entry
    current_sl = position.sl

    if phase == "initial":
        locked_sl = entry + move_to_tp1 * 0.20
        threshold = entry + move_to_tp1 * 0.75
        if price >= threshold:
            if not _is_valid_sl(symbol, "buy", locked_sl):
                logger.warning(f"Trade {order_id}: partial lock SL {locked_sl:.5f} too close to price {price:.5f}")
                return
            modify_position_sl_tp(position.ticket, locked_sl, tp1)
            trade.phase = "partial_lock"
            trade.current_sl = locked_sl
            logger.info(f"Partial lock SL moved | order_id={order_id} locked_sl={locked_sl:.5f} price={price:.5f}")

    elif phase == "partial_lock":
        if price >= tp1:
            locked_sl = entry + move_to_tp1 * 0.30
            if not _is_valid_sl(symbol, "buy", locked_sl):
                logger.warning(f"Trade {order_id}: locked SL {locked_sl:.5f} too close to price {price:.5f}")
                return
            modify_position_sl_tp(position.ticket, locked_sl, tp_final)
            trade.phase = "tp1_hit"
            trade.current_sl = locked_sl
            trade.current_tp = tp_final
            logger.info(f"TP1 reached, extending TP | order_id={order_id} tp_final={tp_final:.5f}")

    elif phase == "tp1_hit":
        trailing_distance = (tp_final - entry) * 0.20
        new_sl = price - trailing_distance
        if new_sl > current_sl:
            if not _is_valid_sl(symbol, "buy", new_sl):
                logger.warning(f"Trade {order_id}: trailing SL {new_sl:.5f} too close to price {price:.5f}")
                return
            if abs(new_sl - current_sl) < SL_CHANGE_THRESHOLD:
                return
            modify_position_sl_tp(position.ticket, new_sl, tp_final)
            trade.current_sl = new_sl
            logger.info(f"Trailing SL updated | order_id={order_id} new_sl={new_sl:.5f} price={price:.5f}")


def _manage_sell_trade(trade: TradeInfo, position) -> None:
    """
    Apply adaptive SL/TP logic for a SELL position (mirror of BUY).
    """
    symbol = trade.symbol
    entry = trade.entry_price
    tp1 = trade.initial_tp1
    tp_final = trade.tp2 if trade.tp2 is not None else tp1
    phase = trade.phase
    order_id = trade.order_id

    price = _get_current_price(symbol, "sell")
    if price is None:
        return

    move_to_tp1 = entry - tp1
    current_sl = position.sl

    if phase == "initial":
        locked_sl = entry - move_to_tp1 * 0.20
        threshold = entry - move_to_tp1 * 0.75
        if price <= threshold:
            if not _is_valid_sl(symbol, "sell", locked_sl):
                logger.warning(f"Trade {order_id}: partial lock SL {locked_sl:.5f} too close to price {price:.5f}")
                return
            modify_position_sl_tp(position.ticket, locked_sl, tp1)
            trade.phase = "partial_lock"
            trade.current_sl = locked_sl
            logger.info(f"Partial lock SL moved | order_id={order_id} locked_sl={locked_sl:.5f} price={price:.5f}")

    elif phase == "partial_lock":
        if price <= tp1:
            locked_sl = entry - move_to_tp1 * 0.30
            if not _is_valid_sl(symbol, "sell", locked_sl):
                logger.warning(f"Trade {order_id}: locked SL {locked_sl:.5f} too close to price {price:.5f}")
                return
            modify_position_sl_tp(position.ticket, locked_sl, tp_final)
            trade.phase = "tp1_hit"
            trade.current_sl = locked_sl
            trade.current_tp = tp_final
            logger.info(f"TP1 reached, extending TP | order_id={order_id} tp_final={tp_final:.5f}")

    elif phase == "tp1_hit":
        trailing_distance = (entry - tp_final) * 0.20
        new_sl = price + trailing_distance
        if new_sl < current_sl:
            if not _is_valid_sl(symbol, "sell", new_sl):
                logger.warning(f"Trade {order_id}: trailing SL {new_sl:.5f} too close to price {price:.5f}")
                return
            if abs(new_sl - current_sl) < SL_CHANGE_THRESHOLD:
                return
            modify_position_sl_tp(position.ticket, new_sl, tp_final)
            trade.current_sl = new_sl
            logger.info(f"Trailing SL updated | order_id={order_id} new_sl={new_sl:.5f} price={price:.5f}")


def manage_open_positions() -> None:
    """
    Background task: scan all open MT5 positions (magic=10001) and apply
    adaptive SL/TP management via the shared active_trades registry.
    """
    settings = get_settings()
    positions = mt5.positions_get()
    if positions is None:
        logger.debug("No open positions found.")
        return

    open_ticket_ids = set()
    for position in positions:
        if position.magic != settings.magic_number:
            continue

        trade = active_trades.get(position.ticket)
        if trade is None:
            continue

        open_ticket_ids.add(position.ticket)

        direction = trade.direction
        if direction == "buy":
            _manage_buy_trade(trade, position)
        elif direction == "sell":
            _manage_sell_trade(trade, position)

    # Unregister any trades that are no longer open in MT5
    stale = [tid for tid in active_trades if tid not in open_ticket_ids]
    for tid in stale:
        unregister_trade(tid)
        logger.info(f"[{tid}] Removed from registry — position no longer open")


def _trade_manager_loop() -> None:
    """Daemon thread loop — runs manage_open_positions every 2 seconds."""
    logger.info("Trade manager thread started.")
    while _manager_running:
        try:
            manage_open_positions()
        except Exception:
            logger.exception("Unexpected error in trade manager loop")
        time.sleep(2.0)
    logger.info("Trade manager thread stopped.")


def start_trade_manager() -> None:
    """Launch the background trade manager thread (daemon)."""
    t = threading.Thread(target=_trade_manager_loop, daemon=True)
    t.start()
    logger.info("Trade manager background thread launched.")


# ── FastAPI app ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifecycle manager: connect to MT5 on startup, disconnect on shutdown.
    """
    logger.info("Starting up MT5 Trade API...")
    try:
        connect_mt5()
    except MT5ConnectionError as e:
        logger.error(f"Failed to connect to MT5 on startup: {e}")
    start_trade_manager()
    start_monitor()
    
    # Auto-start the signal watcher
    logger.info("Auto-starting signal watcher...")
    start_watcher()
    
    yield
    global _manager_running
    _manager_running = False
    stop_monitor()
    
    # Stop the signal watcher on shutdown
    logger.info("Stopping signal watcher...")
    stop_watcher()
    
    logger.info("Shutting down...")
    disconnect_mt5()


app = FastAPI(
    title="MT5 Trade API",
    description="REST API for executing trades via MetaTrader 5",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(ApiKeyAuthMiddleware)


@app.get("/", tags=["Health"])
async def root():
    return {"status": "running", "service": "MT5 Trade API"}


@app.get("/health", tags=["Health"], response_model=dict)
async def health_check():
    return {"status": "healthy"}


@app.post("/watch/start", tags=["Watcher"], response_model=dict)
async def start_watcher_endpoint():
    """
    Start polling SignalTrade for BUY/SELL signals and auto-fire trades to MT5.
    The watcher runs in the background and polls every SIGNALTRADE_POLL_INTERVAL seconds.
    """
    started = start_watcher()
    if started:
        return {"status": "started", **watcher_status()}
    return {"status": "already_running", **watcher_status()}


@app.post("/watch/stop", tags=["Watcher"], response_model=dict)
async def stop_watcher_endpoint():
    """Stop the SignalTrade watcher."""
    stopped = stop_watcher()
    if stopped:
        return {"status": "stopped"}
    return {"status": "not_running"}


@app.get("/watch/status", tags=["Watcher"], response_model=dict)
async def watcher_status_endpoint():
    """Get the current watcher status and last seen signals per pair."""
    return watcher_status()


@app.get("/monitor/status", tags=["Monitor"], response_model=dict)
async def monitor_status_endpoint():
    """Get the current trade monitor (adaptive SL/TP) status and tracked trades."""
    return monitor_status()


@app.get("/trades/active", tags=["Trading"], response_model=dict)
async def active_trades_endpoint():
    """Return the current active_trades registry (adaptive SL/TP tracking)."""
    return {
        "active_trades": {
            str(oid): {
                "order_id": t.order_id,
                "symbol": t.symbol,
                "direction": t.direction,
                "entry": t.entry_price,
                "phase": t.phase,
                "initial_sl": t.initial_sl,
                "current_sl": t.current_sl,
                "initial_tp1": t.initial_tp1,
                "current_tp": t.current_tp,
                "tp2": t.tp2,
            }
            for oid, t in active_trades.items()
        }
    }


@app.post(
    "/trade",
    tags=["Trading"],
    response_model=TradeResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def trade(request: TradeRequest):
    """
    Execute a trade on MT5.

    - **symbol**: Trading symbol (GBPUSD, EURUSD, USDJPY, AUDUSD)
    - **volume**: Trade volume in lots
    - **order_type**: 'buy' or 'sell'
    - **sl**: Stop Loss price
    - **tp**: Take Profit price (TP1 / initial TP)
    - **tp1**: First take profit target (optional, falls back to tp if omitted)
    - **tp_final**: Final take profit target (optional, falls back to tp if omitted)
    """
    equity_ok, equity_info = is_equity_peak_safe()
    if not equity_ok:
        return TradeResponse(
            success=False,
            order_id=None,
            executed_price=None,
            message="Trade blocked: equity dropped 10% from intraday peak",
        )

    if not is_margin_safe()[0]:
        return TradeResponse(
            success=False,
            order_id=None,
            executed_price=None,
            message="Trade blocked: margin usage exceeded 40%",
        )

    drawdown_ok, loss_pct = is_drawdown_safe()
    if not drawdown_ok:
        return TradeResponse(
            success=False,
            order_id=None,
            executed_price=None,
            message="Trade blocked: daily loss limit reached (50%)",
        )

    entry_price = _get_current_price(request.symbol, request.order_type)
    if entry_price is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cannot get current price for spacing check",
        )
    if not should_execute_trade(
        request.symbol,
        request.order_type,
        entry_price,
        active_trades,
    ):
        return TradeResponse(
            success=False,
            order_id=None,
            executed_price=None,
            message="Trade skipped: price too close to existing position",
        )

    try:
        result = open_trade(request)
        register_trade(
            order_id=result.order_id,
            symbol=request.symbol,
            direction=request.order_type,
            entry_price=result.executed_price,
            initial_sl=request.sl,
            initial_tp1=request.tp1 if request.tp1 is not None else request.tp,
            tp2=request.tp_final if request.tp_final is not None else None,
        )
        logger.info(
            f"Trade registered | order_id={result.order_id} symbol={request.symbol} "
            f"direction={request.order_type} entry={result.executed_price} "
            f"tp1={request.tp1 or request.tp} tp2={request.tp_final}"
        )
        return result

    except MT5ConnectionError as e:
        logger.error(f"MT5 connection error: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        )

    except MT5TradeError as e:
        logger.error(f"Trade execution error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    except Exception as e:
        logger.exception("Unexpected error during trade execution")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {e}",
        )


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
