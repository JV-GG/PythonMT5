"""
Application configuration.
All sensitive credentials and tunable parameters are centralized here.
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Any


class Settings(BaseSettings):
    # MT5 Connection
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_terminal_path: str = ""

    # API Security
    api_key: str = ""

    # Trading
    allowed_symbols: str | list[str] = ["GBPUSD", "EURUSD", "USDJPY", "AUDUSD", "BTCUSD", "XAUUSD"]
    default_volume: float = 0.01          # default trade volume in lots
    xauusd_volume: float = 0.01          # legacy, keeping for compatibility
    magic_number: int = 10001
    default_deviation: int = 20
    default_comment: str = "SignalTrade Auto"
    max_positions_per_symbol: int = 2
    max_buy_positions_per_symbol: int = 1
    max_sell_positions_per_symbol: int = 1
    tp_reduction_pct: float = 0.20         # reduce TP and SL by 20% of entry distance (pull closer)
    # SignalTrade integration
    signaltrade_url: str = "http://localhost:3000"
    signaltrade_poll_interval: float = 1.0   # seconds between each poll

    # Trade monitor (adaptive SL/TP)
    monitor_poll_interval: float = 1.0  # seconds between each monitor cycle
    monitor_tp1_proximity: float = 5.0  # how close price must be to TP1 to trigger Phase 2 (in pips, e.g. 5.0)

    # Adaptive SL/TP Customization
    early_risk_reduction_enabled: bool = True
    early_risk_cut_trigger_pct: float = 0.20   # 20% of TP1 move -> cut SL risk by 50%
    early_breakeven_trigger_pct: float = 0.30  # 30% of TP1 move -> move SL to Breakeven (Entry)
    phase1_trigger_pct: float = 0.60       # 60% of TP1 move -> trigger Phase 1 lock
    phase1_lock_pct: float = 0.50          # percentage of TP1 distance locked as profit at Phase 1 trigger
    phase2_lock_pct: float = 0.70          # percentage of TP1 distance locked as profit when TP1 is hit
    trailing_sl_pct: float = 0.20          # trailing SL distance as percentage of entry-to-TP2 total move

    # Trading Session Restrictions
    session_restrictions_enabled: bool = True
    allowed_sessions: str | list[str] = ["london", "asia"]
    avoid_sessions: str | list[str] = ["us"]

    # Local Time Restrictions
    local_time_restriction_enabled: bool = True
    local_time_start: str = "10:00"
    local_time_end: str = "20:00"

    # XAUUSD Specific Volumes
    xauusd_weekday_volume: float = 0.01
    xauusd_friday_volume: float = 0.01

    # Daily Profit Target Circuit Breaker
    daily_profit_target_enabled: bool = True
    daily_profit_target_usd: float = 50.0   # Stop trading if profit reaches $50 USD
    daily_profit_target_pct: float = 0.05   # Stop trading if profit reaches 5% of day's start balance

    # Dynamic Daily Drawdown Circuit Breaker
    daily_drawdown_enabled: bool = True
    daily_drawdown_pct: float = 0.10        # 10% of day's peak/starting balance
    daily_drawdown_min_usd: float = 100.0   # $100 USD minimum threshold

    # Logging
    log_file: str = "trading.log"

    @field_validator("allowed_symbols", "allowed_sessions", "avoid_sessions", mode="before")
    @classmethod
    def parse_list(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

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
