"""
MetaTrader 5 service layer.
Handles MT5 connection, trade execution, spacing checks, and risk management.
"""
import time

import MetaTrader5 as mt5
import logging
from typing import Any

from config import get_settings
from schemas import TradeInfo, PHASE_1, TradeRequest, TradeResponse


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

    init_kwargs = {}
    if settings.mt5_terminal_path:
        init_kwargs["path"] = settings.mt5_terminal_path

    if not mt5.initialize(**init_kwargs):
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

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise MT5TradeError(f"Failed to get tick data for '{symbol}'.")

    # ── Final pre-execution sanity check ─────────────────────────────────────────
    # Fail fast with a clear message rather than sending garbage to MT5.
    if request.order_type == "buy":
        if not (request.sl < tick.ask < request.tp):
            raise MT5TradeError(
                f"Pre-execution sanity failed for BUY {symbol}: "
                f"SL={request.sl} must be < Ask={tick.ask:.5f} must be < TP={request.tp}. "
                f"Likely cause: SignalTrade sent SL/TP as pips instead of price levels."
            )
    else:
        if not (request.tp < tick.bid < request.sl):
            raise MT5TradeError(
                f"Pre-execution sanity failed for SELL {symbol}: "
                f"TP={request.tp} must be < Bid={tick.bid:.5f} must be < SL={request.sl}. "
                f"Likely cause: SignalTrade sent SL/TP as pips instead of price levels."
            )

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


# ---------------------------------------------------------------------------
# ATR-based dynamic spacing
# ---------------------------------------------------------------------------

# ATR cache: {symbol: {"value": float, "timestamp": float}}
# Prevents recalculating ATR on every single request.
_atr_cache: dict[str, dict[str, float]] = {}

# How long (seconds) a cached ATR value is valid before refresh.
ATR_CACHE_TTL = 20.0

# Multiplier applied to ATR to derive the minimum distance per symbol.
# Higher = more conservative spacing; lower = tighter spacing allowed.
symbol_atr_multiplier: dict[str, float] = {
    "GBPUSD": 1.0,
    "EURUSD": 1.0,
    "USDJPY": 1.0,
    "AUDUSD": 1.0,
}

# Fallback minimum distances (used if ATR fetch fails).
FALLBACK_MIN_DISTANCE: dict[str, float] = {
    "GBPUSD": 0.003,
    "EURUSD": 0.003,
    "USDJPY": 0.3,
    "AUDUSD": 0.003,
}


def get_atr(symbol: str, timeframe: int = mt5.TIMEFRAME_M5, period: int = 14) -> float | None:
    """
    Calculate the Average True Range (ATR) for a given symbol.

    ATR is computed from M5 bars using Wilder's smoothing method (simple rolling mean
    of the True Range over `period` bars), matching the standard MT5 ATR indicator.

    Results are cached for ATR_CACHE_TTL seconds to avoid excessive MT5 calls.

    Args:
        symbol:    MT5 symbol, e.g. "BTCUSD"
        timeframe: MT5 timeframe constant (default: M5)
        period:    ATR lookback period (default: 14)

    Returns:
        Latest ATR value, or None if the symbol cannot be fetched.
    """
    now = time.time()
    cached = _atr_cache.get(symbol)

    if cached is not None and (now - cached["timestamp"]) < ATR_CACHE_TTL:
        return cached["value"]

    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, period + 1)
    if rates is None or len(rates) < period + 1:
        logger.warning(f"ATR fetch failed for {symbol}: insufficient data")
        return None

    # MT5 on Windows returns numpy.void rows that don't expose named attributes.
    # Convert each row to a plain dict so .high / .low / .close always work.
    if hasattr(rates[0], "tolist"):
        rates = [row.tolist() for row in rates]

    tr_list: list[float] = []
    for i in range(1, len(rates)):
        row = rates[i]
        high = row.high if hasattr(row, "high") else row[2]
        low = row.low if hasattr(row, "low") else row[3]
        prev_close = rates[i - 1].close if hasattr(rates[i - 1], "close") else rates[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

    if len(tr_list) < period:
        logger.warning(f"ATR fetch failed for {symbol}: not enough TR samples")
        return None

    atr = sum(tr_list[-period:]) / period

    if atr <= 0:
        logger.warning(f"ATR is zero or negative for {symbol}: {atr}")
        return None

    _atr_cache[symbol] = {"value": atr, "timestamp": now}
    logger.info(f"ATR={atr:.5f} cached for {symbol} (TTL={ATR_CACHE_TTL}s)")
    return atr


def get_dynamic_min_distance(symbol: str) -> float:
    """
    Return the minimum required price distance for a symbol using ATR.

    Formula: min_distance = ATR * symbol_atr_multiplier[symbol]

    Falls back to fixed values if ATR cannot be retrieved or is invalid.

    Args:
        symbol: MT5 symbol, e.g. "BTCUSD"

    Returns:
        Minimum distance in price units.
    """
    atr = get_atr(symbol)
    multiplier = symbol_atr_multiplier.get(symbol, 1.0)

    if atr is not None:
        min_dist = atr * multiplier
        logger.info(f"Dynamic spacing | symbol={symbol} atr={atr:.5f} multiplier={multiplier} min_distance={min_dist:.5f}")
        return min_dist

    # Fallback to fixed values
    fallback = FALLBACK_MIN_DISTANCE.get(symbol, 0.0)
    logger.warning(f"ATR unavailable for {symbol}, using fallback min_distance={fallback}")
    return fallback


def should_execute_trade(
    symbol: str,
    direction: str,
    new_entry_price: float,
    active_trades_ref: dict[int, TradeInfo],
) -> bool:
    """
    Decide whether a new trade should be allowed based on ATR-based dynamic
    spacing from existing open positions of the same symbol and direction.

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
    min_dist = get_dynamic_min_distance(symbol)

    # 1. Check active_trades registry (preferred — no MT5 round-trip)
    for order_id, trade in active_trades_ref.items():
        if trade.symbol != symbol or trade.direction != direction:
            continue
        existing_price = trade.entry_price
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


# ---------------------------------------------------------------------------
# Global risk management state (session-scoped)
# ---------------------------------------------------------------------------

_daily_start_balance: float | None = None
_daily_loss_limit_hit: bool = False
_last_reset_time: float | None = None
DAILY_RESET_INTERVAL = 86400.0   # 24 hours in seconds

# Equity peak tracking (resets every 24 hours with daily metrics)
_peak_equity: float | None = None
EQUITY_DRAWDOWN_LIMIT = 0.10     # 10% drop from peak → block trading


def _get_account_info() -> dict | None:
    """
    Pull live account metrics from MT5.
    Returns None if MT5 is unavailable or disconnected.
    """
    info = mt5.account_info()
    if info is None:
        logger.error("Failed to fetch MT5 account info")
        return None
    return {
        "balance": info.balance,
        "equity": info.equity,
        "margin": info.margin,
        "free_margin": info.margin_free,
    }


def _reset_daily_metrics() -> None:
    """
    Snapshot the current balance as the new 24-hour reference point.
    Clears the drawdown flag so the system can trade again after the window resets.
    """
    global _daily_start_balance, _daily_loss_limit_hit, _last_reset_time, _peak_equity
    now = time.time()
    if _last_reset_time is None or (now - _last_reset_time) >= DAILY_RESET_INTERVAL:
        info = _get_account_info()
        if info is None:
            return
        _daily_start_balance = info["balance"]
        _daily_loss_limit_hit = False
        _peak_equity = None          # reset equity peak on new session
        _last_reset_time = now
        logger.info(
            f"Daily metrics reset | balance={_daily_start_balance:.2f} "
            f"(next reset in {DAILY_RESET_INTERVAL / 3600:.1f}h)"
        )


def is_margin_safe(margin_threshold: float = 0.40) -> tuple[bool, dict | None]:
    """
    Block new trades if current margin usage is at or above the threshold.

    margin_usage = margin_used / (margin_used + free_margin)
    If margin_usage >= threshold → unsafe.

    Returns:
        (True, account_info)   → trading is safe
        (False, account_info)   → margin usage too high
    """
    info = _get_account_info()
    if info is None:
        return False, None

    total_margin = info["margin"] + info["free_margin"]
    if total_margin <= 0:
        # No open positions — trivially safe
        return True, info

    margin_usage = info["margin"] / total_margin

    if margin_usage >= margin_threshold:
        logger.warning(
            f"Margin usage too high | usage={margin_usage:.2%} threshold={margin_threshold:.2%} "
            f"margin={info['margin']:.2f} free_margin={info['free_margin']:.2f}"
        )
        return False, info

    logger.info(
        f"Margin check passed | usage={margin_usage:.2%} threshold={margin_threshold:.2%} "
        f"margin={info['margin']:.2f} free_margin={info['free_margin']:.2f}"
    )
    return True, info


def is_drawdown_safe(drawdown_threshold: float = 0.50) -> tuple[bool, float | None]:
    """
    Block new trades if the rolling 24-hour session loss reaches the threshold.

    loss_percent = (daily_start_balance - current_balance) / daily_start_balance
    If loss_percent >= threshold → trading paused for the rest of the window.

    Returns:
        (True, None)           → trading is safe
        (False, loss_percent)  → drawdown limit hit
    """
    global _daily_loss_limit_hit
    _reset_daily_metrics()

    if _daily_start_balance is None or _daily_start_balance <= 0:
        logger.warning("Daily start balance unavailable — allowing trades with caution")
        return True, None

    info = _get_account_info()
    if info is None:
        return False, None

    current_balance = info["balance"]
    loss = _daily_start_balance - current_balance
    loss_percent = loss / _daily_start_balance

    if _daily_loss_limit_hit:
        remaining = DAILY_RESET_INTERVAL - (time.time() - _last_reset_time)
        logger.warning(
            f"Daily loss limit already hit | "
            f"(will reset in {remaining:.0f}s)"
        )
        return False, loss_percent

    if loss_percent >= drawdown_threshold:
        _daily_loss_limit_hit = True
        logger.warning(
            f"Daily loss limit reached | loss={loss:.2f} ({loss_percent:.2%}) "
            f"daily_start={_daily_start_balance:.2f} current={current_balance:.2f}"
        )
        return False, loss_percent

    if loss > 0:
        logger.info(
            f"Drawdown check passed | loss={loss:.2f} ({loss_percent:.2%}) "
            f"daily_start={_daily_start_balance:.2f}"
        )

    return True, None


def is_equity_peak_safe(equity_drawdown_limit: float = EQUITY_DRAWDOWN_LIMIT) -> tuple[bool, dict | None]:
    """
    Track the intraday peak equity and block new trades if equity has dropped
    more than equity_drawdown_limit (default 10%) from that peak.

    The peak is updated every call so that a new higher equity reading resets
    thewatermark upward. The metric resets to None on the 24-hour boundary
    (handled by _reset_daily_metrics).

    Args:
        equity_drawdown_limit: fraction of peak equity that triggers a block (default 0.10)

    Returns:
        (True,  info_dict)  → trading is allowed
        (False, info_dict)  → equity has dropped too far from peak
    """
    info = _get_account_info()
    if info is None:
        return False, None

    current_equity = info["equity"]

    # Initialize peak on first call or after daily reset
    global _peak_equity
    if _peak_equity is None or current_equity > _peak_equity:
        _peak_equity = current_equity
        logger.info(
            f"Equity peak updated | peak={_peak_equity:.2f} current={current_equity:.2f}"
        )

    # Guard against division by zero (equity should never be 0 in practice)
    if _peak_equity <= 0:
        logger.warning("Peak equity is zero or negative — allowing trades with caution")
        return True, info

    drawdown = (_peak_equity - current_equity) / _peak_equity

    if drawdown >= equity_drawdown_limit:
        logger.warning(
            f"Equity drawdown too deep | "
            f"peak={_peak_equity:.2f} current={current_equity:.2f} "
            f"drawdown={drawdown:.2%} limit={equity_drawdown_limit:.2%}"
        )
        return False, info

    logger.info(
        f"Equity peak check passed | "
        f"peak={_peak_equity:.2f} current={current_equity:.2f} "
        f"drawdown={drawdown:.2%}"
    )
    return True, info


# ── Trade registry ──────────────────────────────────────────────────────────────
# Single source of truth for all open trade metadata used by the adaptive SL/TP
# managers (threaded manager in main.py and async monitor in trade_monitor.py).
# Key: order_id (int), Value: TradeInfo

from schemas import TradeInfo, PHASE_1

active_trades: dict[int, TradeInfo] = {}


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
    Register an open trade so the adaptive SL/TP managers can track it.
    Call this after a trade is successfully opened.
    """
    active_trades[order_id] = TradeInfo(
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


def unregister_trade(order_id: int) -> bool:
    """
    Remove a trade from the registry (e.g. when it is closed).
    Returns True if the trade was removed, False if it wasn't tracked.
    """
    return active_trades.pop(order_id, None) is not None


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
