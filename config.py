"""
Application configuration.
All sensitive credentials and tunable parameters are centralized here.
"""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # MT5 Connection
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_terminal_path: str = ""

    # API Security
    api_key: str = ""

    # Trading
    allowed_symbols: list[str] = ["GBPUSD", "EURUSD", "USDJPY", "AUDUSD", "BTCUSD", "XAUUSD"]
    default_volume: float = 0.01          # default trade volume in lots
    xauusd_volume: float = 0.01          # legacy, keeping for compatibility
    magic_number: int = 10001
    default_deviation: int = 20
    default_comment: str = "SignalTrade Auto"
    max_positions_per_symbol: int = 2
    max_buy_positions_per_symbol: int = 1
    max_sell_positions_per_symbol: int = 1
    tp_reduction_pct: float = 0.10         # reduce TP by 10% of entry→TP distance (spread buffer)
    # SignalTrade integration
    signaltrade_url: str = "http://localhost:3000"
    signaltrade_poll_interval: int = 60  # seconds between each poll

    # Trade monitor (adaptive SL/TP)
    monitor_poll_interval: int = 2   # seconds between each monitor cycle
    monitor_tp1_proximity: float = 5.0  # how close price must be to TP1 to trigger Phase 2 (in pips, e.g. 5.0)

    # Adaptive SL/TP Customization
    phase1_trigger_pct: float = 0.75       # threshold percentage of entry-to-TP1 distance to trigger Phase 1
    phase1_lock_pct: float = 0.50          # percentage of TP1 distance locked as profit at Phase 1 trigger
    phase2_lock_pct: float = 0.70          # percentage of TP1 distance locked as profit when TP1 is hit
    trailing_sl_pct: float = 0.20          # trailing SL distance as percentage of entry-to-TP2 total move

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
