"""
FastAPI application entry point.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from config import get_settings
from auth import ApiKeyAuthMiddleware
from schemas import TradeRequest, TradeResponse, ErrorResponse
from mt5_service import (
    connect_mt5,
    disconnect_mt5,
    open_trade,
    MT5ConnectionError,
    MT5TradeError,
)
from signal_watcher import start_watcher, stop_watcher, watcher_status


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


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
    yield
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
    - **tp**: Take Profit price
    """
    try:
        result = open_trade(request)
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
