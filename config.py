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

    # API Security
    api_key: str = ""

    # Trading
    allowed_symbols: list[str] = ["BTCUSD", "GBPUSD", "USDJPY", "XAUUSD"]
    default_volume: float = 0.1          # used for BTCUSD, GBPUSD, USDJPY
    xauusd_volume: float = 0.01          # used for XAUUSD
    magic_number: int = 10001
    default_deviation: int = 20
    default_comment: str = "SignalTrade Auto"
    # Multiplier applied to the live spread (in points) to build a buffer around
    # SL/TP. E.g. 1.0 means use exactly 1x the spread as buffer. Set to 0 to
    # disable spread adjustment entirely.
    spread_buffer_multiplier: float = 1.0

    # SignalTrade integration
    signaltrade_url: str = "http://localhost:3000"
    signaltrade_poll_interval: int = 60  # seconds between each poll

    # Trade monitor (adaptive SL/TP)
    monitor_poll_interval: int = 2   # seconds between each monitor cycle
    monitor_tp1_proximity: float = 0.5  # how close price must be to TP1 to trigger Phase 2

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
