"""
MetaTrader 5 service layer.
Handles MT5 connection and trade execution.
"""
import MetaTrader5 as mt5
import logging
from typing import Any

from config import get_settings
from schemas import TradeRequest, TradeResponse


logger = logging.getLogger(__name__)


class MT5ConnectionError(Exception):
    """Raised when MT5 connection fails."""
    pass


class MT5TradeError(Exception):
    """Raised when a trade execution fails."""
    pass


def connect_mt5() -> bool:
    """
    Initialize MT5 connection using credentials from config.
    Returns True on success, raises MT5ConnectionError on failure.
    """
    settings = get_settings()

    if not settings.mt5_login or not settings.mt5_password or not settings.mt5_server:
        raise MT5ConnectionError(
            "MT5 credentials not configured. "
            "Set MT5_LOGIN, MT5_PASSWORD, MT5_SERVER in .env"
        )

    logger.info(f"Connecting to MT5 server: {settings.mt5_server}")

    if not mt5.initialize():
        error_code = mt5.last_error()
        raise MT5ConnectionError(
            f"MT5 initialize() failed. Error code: {error_code}"
        )

    logged_in = mt5.login(
        login=settings.mt5_login,
        password=settings.mt5_password,
        server=settings.mt5_server,
    )

    if not logged_in:
        error_code = mt5.last_error()
        mt5.shutdown()
        raise MT5ConnectionError(
            f"MT5 login failed. Error code: {error_code}"
        )

    logger.info(f"MT5 connected successfully. Account: {settings.mt5_login}")
    return True


def disconnect_mt5() -> None:
    """Shutdown MT5 connection."""
    mt5.shutdown()
    logger.info("MT5 disconnected.")


def open_trade(request: TradeRequest) -> TradeResponse:
    """
    Execute a trade on MT5.
    Returns TradeResponse with order_id and executed price on success.
    Raises MT5TradeError on failure.
    """
    settings = get_settings()

    symbol = request.symbol
    if not mt5.terminal_info():
        raise MT5ConnectionError("MT5 is not initialized. Call connect_mt5() first.")

    if not mt5.symbol_select(symbol):
        raise MT5TradeError(f"Symbol '{symbol}' not found or cannot be selected in MT5.")

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        raise MT5TradeError(f"Failed to get symbol info for '{symbol}'.")

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise MT5TradeError(f"Failed to get tick data for '{symbol}'.")

    action_type = mt5.TRADE_ACTION_DEAL
    order_type = mt5.ORDER_TYPE_BUY if request.order_type == "buy" else mt5.ORDER_TYPE_SELL
    price = tick.ask if request.order_type == "buy" else tick.bid
    order_type_filling = mt5.ORDER_FILLING_IOC

    trade_request: dict[str, Any] = {
        "action": action_type,
        "symbol": symbol,
        "volume": request.volume,
        "type": order_type,
        "price": price,
        "sl": request.sl,
        "tp": request.tp,
        "deviation": settings.default_deviation,
        "magic": settings.magic_number,
        "comment": settings.default_comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": order_type_filling,
    }

    logger.info(
        f"Executing trade | symbol={symbol} volume={request.volume} "
        f"type={request.order_type} price={price} sl={request.sl} tp={request.tp}"
    )

    result = mt5.order_send(trade_request)

    if result is None:
        raise MT5TradeError(
            f"order_send() returned None. Error code: {mt5.last_error()}"
        )

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise MT5TradeError(
            f"Trade failed. Retcode={result.retcode} ({result.comment})"
        )

    logger.info(
        f"Trade executed | order_id={result.order} price={result.price} "
        f"volume={result.volume} retcode={result.retcode}"
    )

    return TradeResponse(
        success=True,
        order_id=result.order,
        executed_price=result.price,
        message=f"Order {result.order} executed successfully.",
    )
