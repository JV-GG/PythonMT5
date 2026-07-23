"""
Type stub for MetaTrader5 package.

MetaTrader5 is a C extension (.pyd) without bundled type stubs.
This stub declares only the symbols actually used by this project
so that Pyrefly / Pyright / mypy can resolve them.
"""
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────────

# Trade actions
TRADE_ACTION_DEAL: int
TRADE_ACTION_SLTP: int

# Order types
ORDER_TYPE_BUY: int
ORDER_TYPE_SELL: int

# Position types
POSITION_TYPE_BUY: int
POSITION_TYPE_SELL: int

# Order filling modes
ORDER_FILLING_IOC: int

# Order time
ORDER_TIME_GTC: int

# Trade return codes
TRADE_RETCODE_DONE: int

# Timeframes
TIMEFRAME_M5: int

# ── Functions ────────────────────────────────────────────────────────────────

def initialize(path: str = ..., **kwargs: Any) -> bool: ...
def shutdown() -> None: ...
def login(login: int, password: str = ..., server: str = ..., timeout: int = ...) -> bool: ...
def last_error() -> tuple[int, str]: ...
def terminal_info() -> Any: ...
def account_info() -> Any: ...
def symbol_select(symbol: str, enable: bool = ...) -> bool: ...
def symbol_info(symbol: str) -> Any: ...
def symbol_info_tick(symbol: str) -> Any: ...
def positions_get(symbol: str = ..., ticket: int = ..., group: str = ...) -> tuple[Any, ...] | None: ...
def order_send(request: dict[str, Any]) -> Any: ...
def copy_rates_from_pos(symbol: str, timeframe: int, start_pos: int, count: int) -> Any: ...
