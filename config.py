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
    allowed_symbols: list[str] = ["BTCUSD", "GBPUSD", "USDJPY"]
    default_volume: float = 0.1
    magic_number: int = 10001
    default_deviation: int = 20
    default_comment: str = "SignalTrade Auto"

    # SignalTrade integration
    signaltrade_url: str = "http://localhost:3000"
    signaltrade_poll_interval: int = 60  # seconds between each poll

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
