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


# Minimum distance (in price units) required between new entry and existing
# positions of the same symbol and direction before a new trade is allowed.
symbol_min_distance: dict[str, float] = {
    "BTCUSD": 300,
    "XAUUSD": 10,
    "USDJPY": 0.3,
    "GBPUSD": 0.003,
}


def should_execute_trade(
    symbol: str,
    direction: str,
    new_entry_price: float,
    active_trades_ref: dict[int, dict],
) -> bool:
    """
    Decide whether a new trade should be allowed based on spacing from existing
    open positions of the same symbol and direction.

    Reads from the active_trades registry first (avoids extra MT5 calls when
    the position is already tracked), then falls back to mt5.positions_get()
    for any positions not yet registered.

    Args:
        symbol:           MT5 symbol, e.g. "BTCUSD"
        direction:        "buy" or "sell"
        new_entry_price:  proposed entry price from the signal
        active_trades_ref: reference to the shared active_trades dict

    Returns:
        True  → trade is allowed (sufficient distance from all existing positions)
        False → trade should be skipped (price too close to an existing position)
    """
    settings = get_settings()
    min_dist = symbol_min_distance.get(symbol, 0.0)

    # 1. Check active_trades registry (preferred — no MT5 round-trip)
    for order_id, trade in active_trades_ref.items():
        if trade["symbol"] != symbol or trade["direction"] != direction:
            continue
        existing_price = trade["entry_price"]
        if direction == "buy":
            if new_entry_price <= existing_price - min_dist:
                logger.info(
                    f"Trade allowed (active_trades) | symbol={symbol} new_entry={new_entry_price} "
                    f"existing={existing_price} distance={abs(new_entry_price - existing_price):.5f}"
                )
                return True
        else:
            if new_entry_price >= existing_price + min_dist:
                logger.info(
                    f"Trade allowed (active_trades) | symbol={symbol} new_entry={new_entry_price} "
                    f"existing={existing_price} distance={abs(new_entry_price - existing_price):.5f}"
                )
                return True

    # 2. Fall back to MT5 positions (covers trades opened outside this system)
    positions = mt5.positions_get()
    if positions is None:
        return True

    has_conflicting = False
    for position in positions:
        if position.symbol != symbol or position.magic != settings.magic_number:
            continue
        pos_dir = "buy" if position.type == mt5.ORDER_TYPE_BUY else "sell"
        if pos_dir != direction:
            continue
        has_conflicting = True
        existing_price = position.price_open
        if direction == "buy":
            if new_entry_price <= existing_price - min_dist:
                logger.info(
                    f"Trade allowed (MT5) | symbol={symbol} new_entry={new_entry_price} "
                    f"existing={existing_price} distance={abs(new_entry_price - existing_price):.5f}"
                )
                return True
        else:
            if new_entry_price >= existing_price + min_dist:
                logger.info(
                    f"Trade allowed (MT5) | symbol={symbol} new_entry={new_entry_price} "
                    f"existing={existing_price} distance={abs(new_entry_price - existing_price):.5f}"
                )
                return True

    if has_conflicting:
        logger.warning(
            f"Trade skipped: too close to existing position | "
            f"symbol={symbol} direction={direction} new_entry={new_entry_price} "
            f"min_distance={min_dist}"
        )
        return False

    logger.info(f"Trade allowed: no conflicting positions | symbol={symbol}")
    return True


def modify_position_sl_tp(position_ticket: int, new_sl: float, new_tp: float | None = None) -> dict:
    """
    Modify SL/TP of an open position.

    Args:
        position_ticket: MT5 position ticket number
        new_sl: New stop loss price
        new_tp: New take profit price (optional — keeps existing TP if None)

    Returns:
        Result dict from mt5.order_send
    """
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position_ticket,
        "sl": new_sl,
        "tp": new_tp if new_tp is not None else 0.0,
    }
    logger.info(
        f"Modifying position {position_ticket} | sl={new_sl} tp={new_tp}"
    )
    result = mt5.order_send(request)
    if result is None:
        logger.error(f"Modify returned None. Error: {mt5.last_error()}")
        return {}
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.warning(
            f"Modify failed retcode={result.retcode} comment={result.comment}"
        )
    else:
        logger.info(
            f"Position {position_ticket} modified successfully | sl={new_sl} tp={new_tp}"
        )
    return result
