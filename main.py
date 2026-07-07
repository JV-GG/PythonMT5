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
    load_active_trades,
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

# Thread-safe flag to stop the trade manager loop (deprecated, consolidated into trade_monitor.py)


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
    
    # Restore active trades from persistence
    load_active_trades()
    
    start_monitor()
    
    # Auto-start the signal watcher
    logger.info("Auto-starting signal watcher...")
    start_watcher()
    
    yield
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

    - **symbol**: Trading symbol (GBPUSD, EURUSD, USDJPY, AUDUSD, BTCUSD)
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
            message="Trade skipped: maximum active positions limit reached for this symbol",
        )

    try:
        result = open_trade(request)
        if result.order_id is None or result.executed_price is None:
            raise MT5TradeError(
                f"Trade request returned success but missing order_id ({result.order_id}) "
                f"or executed_price ({result.executed_price})"
            )
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
