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
from schemas import TradeRequest, TradeResponse, ErrorResponse
from mt5_service import (
    connect_mt5,
    disconnect_mt5,
    open_trade,
    modify_position_sl_tp,
    MT5ConnectionError,
    MT5TradeError,
)
from signal_watcher import start_watcher, stop_watcher, watcher_status, active_trades


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


# ---------------------------------------------------------------------------
# Trade execution + registration
# ---------------------------------------------------------------------------

def _register_trade(request: TradeRequest, order_id: int, executed_price: float) -> None:
    """
    Register a successfully executed trade into the active_trades registry
    for the adaptive SL/TP manager to track.
    """
    active_trades[order_id] = {
        "order_id": order_id,
        "symbol": request.symbol,
        "entry_price": executed_price,
        "direction": request.order_type,
        "sl": request.sl,
        "tp1": request.tp1 if request.tp1 is not None else request.tp,
        "tp_final": request.tp_final if request.tp_final is not None else request.tp,
        "stage": "initial",
    }
    logger.info(
        f"Registered trade {order_id} in active_trades | "
        f"symbol={request.symbol} entry={executed_price} "
        f"direction={request.order_type} tp1={active_trades[order_id]['tp1']} "
        f"tp_final={active_trades[order_id]['tp_final']} stage=initial"
    )


# ---------------------------------------------------------------------------
# Adaptive SL/TP management
# ---------------------------------------------------------------------------

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


def _manage_buy_trade(trade: dict, position) -> None:
    """
    Apply adaptive SL/TP logic for a BUY position.

    Stages:
      initial  → breakeven  (75% of move to TP1)
      breakeven → tp1_hit   (price reaches TP1)
      tp1_hit  → trailing   (SL trails upward)
    """
    symbol = trade["symbol"]
    entry = trade["entry_price"]
    tp1 = trade["tp1"]
    tp_final = trade["tp_final"]
    stage = trade["stage"]
    order_id = trade["order_id"]

    price = _get_current_price(symbol, "buy")
    if price is None:
        return

    move_to_tp1 = tp1 - entry
    current_sl = position.sl

    if stage == "initial":
        # 75% of the way to TP1 → move SL to breakeven
        threshold = entry + move_to_tp1 * 0.75
        if price >= threshold:
            if not _is_valid_sl(symbol, "buy", entry):
                logger.warning(f"Trade {order_id}: breakeven SL {entry} is too close to price {price}")
                return
            modify_position_sl_tp(position.ticket, entry, tp1)
            active_trades[order_id]["sl"] = entry
            active_trades[order_id]["stage"] = "breakeven"
            logger.info(f"Moved SL to breakeven | order_id={order_id} price={price}")

    elif stage == "breakeven":
        # Price reached TP1 → extend TP, lock partial profit via SL
        if price >= tp1:
            locked_sl = entry + move_to_tp1 * 0.30
            if not _is_valid_sl(symbol, "buy", locked_sl):
                logger.warning(f"Trade {order_id}: locked SL {locked_sl} is too close to price {price}")
                return
            modify_position_sl_tp(position.ticket, locked_sl, tp_final)
            active_trades[order_id]["sl"] = locked_sl
            active_trades[order_id]["stage"] = "tp1_hit"
            logger.info(f"TP1 reached, extending TP | order_id={order_id} tp_final={tp_final}")

    elif stage == "tp1_hit":
        # Trail SL upward: new_sl = current_price - 20% of total target move
        trailing_distance = (tp_final - entry) * 0.20
        new_sl = price - trailing_distance
        if new_sl > current_sl:
            if not _is_valid_sl(symbol, "buy", new_sl):
                logger.warning(f"Trade {order_id}: trailing SL {new_sl} is too close to price {price}")
                return
            if abs(new_sl - current_sl) < SL_CHANGE_THRESHOLD:
                return
            modify_position_sl_tp(position.ticket, new_sl, tp_final)
            active_trades[order_id]["sl"] = new_sl
            logger.info(f"Trailing SL updated | order_id={order_id} new_sl={new_sl:.5f} price={price:.5f}")


def _manage_sell_trade(trade: dict, position) -> None:
    """
    Apply adaptive SL/TP logic for a SELL position (mirror of BUY).

    Stages:
      initial  → breakeven  (75% of move to TP1, price moving DOWN)
      breakeven → tp1_hit   (price drops to or below TP1)
      tp1_hit  → trailing   (SL trails downward)
    """
    symbol = trade["symbol"]
    entry = trade["entry_price"]
    tp1 = trade["tp1"]
    tp_final = trade["tp_final"]
    stage = trade["stage"]
    order_id = trade["order_id"]

    price = _get_current_price(symbol, "sell")
    if price is None:
        return

    move_to_tp1 = entry - tp1
    current_sl = position.sl

    if stage == "initial":
        # 75% of the way to TP1 (price dropping toward tp1)
        threshold = entry - move_to_tp1 * 0.75
        if price <= threshold:
            if not _is_valid_sl(symbol, "sell", entry):
                logger.warning(f"Trade {order_id}: breakeven SL {entry} is too close to price {price}")
                return
            modify_position_sl_tp(position.ticket, entry, tp1)
            active_trades[order_id]["sl"] = entry
            active_trades[order_id]["stage"] = "breakeven"
            logger.info(f"Moved SL to breakeven | order_id={order_id} price={price}")

    elif stage == "breakeven":
        # Price reached TP1 → extend TP, lock partial profit via SL
        if price <= tp1:
            locked_sl = entry - move_to_tp1 * 0.30
            if not _is_valid_sl(symbol, "sell", locked_sl):
                logger.warning(f"Trade {order_id}: locked SL {locked_sl} is too close to price {price}")
                return
            modify_position_sl_tp(position.ticket, locked_sl, tp_final)
            active_trades[order_id]["sl"] = locked_sl
            active_trades[order_id]["stage"] = "tp1_hit"
            logger.info(f"TP1 reached, extending TP | order_id={order_id} tp_final={tp_final}")

    elif stage == "tp1_hit":
        # Trail SL downward: new_sl = current_price + 20% of total target move
        trailing_distance = (entry - tp_final) * 0.20
        new_sl = price + trailing_distance
        if new_sl < current_sl:
            if not _is_valid_sl(symbol, "sell", new_sl):
                logger.warning(f"Trade {order_id}: trailing SL {new_sl} is too close to price {price}")
                return
            if abs(new_sl - current_sl) < SL_CHANGE_THRESHOLD:
                return
            modify_position_sl_tp(position.ticket, new_sl, tp_final)
            active_trades[order_id]["sl"] = new_sl
            logger.info(f"Trailing SL updated | order_id={order_id} new_sl={new_sl:.5f} price={price:.5f}")


def manage_open_positions() -> None:
    """
    Background task: scan all open MT5 positions (magic=10001) and apply
    adaptive SL/TP management via the active_trades registry.
    """
    settings = get_settings()
    positions = mt5.positions_get()
    if positions is None:
        logger.debug("No open positions found.")
        return

    for position in positions:
        # Only manage positions placed by this system (magic == 10001)
        if position.magic != settings.magic_number:
            continue

        trade = active_trades.get(position.ticket)
        if trade is None:
            continue

        direction = trade["direction"]
        if direction == "buy":
            _manage_buy_trade(trade, position)
        elif direction == "sell":
            _manage_sell_trade(trade, position)


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


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

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
    yield
    global _manager_running
    _manager_running = False
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


@app.get("/trades/active", tags=["Trading"], response_model=dict)
async def active_trades_endpoint():
    """Return the current active_trades registry (adaptive SL/TP tracking)."""
    return {
        "active_trades": {
            str(oid): info for oid, info in active_trades.items()
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

    - **symbol**: Trading symbol (BTCUSD, USDJPY, GBPUSD)
    - **volume**: Trade volume in lots
    - **order_type**: 'buy' or 'sell'
    - **sl**: Stop Loss price
    - **tp**: Take Profit price (TP1 / initial TP)
    - **tp1**: First take profit target (optional, falls back to tp if omitted)
    - **tp_final**: Final take profit target (optional, falls back to tp if omitted)
    """
    try:
        result = open_trade(request)
        _register_trade(request, result.order_id, result.executed_price)
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
