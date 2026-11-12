"""
Application configuration.
All sensitive credentials and tunable parameters are centralized here.
"""
import re

from pydantic import field_validator
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Any


# Names brokers use instead of the pair code, with an optional USD and lowercase account suffix (GOLD, Goldm, SILVERUSD)
_SYMBOL_ALIASES = {"GOLD": "XAUUSD", "SILVER": "XAGUSD", "BITCOIN": "BTCUSD"}
_ALIAS_NAME = re.compile(r"((?i:GOLD|SILVER|BITCOIN))(?i:USD)?[a-z]*")


def canonical_symbol(symbol: str) -> str:
    """
    Reduce any broker's name for an instrument to its base code, so XAUUSD.m, XAUUSD-STD, XAUUSD+, XAUUSDm,
    the cent names XAUUSD.sc, XAUUSDc, XAUUSD.c and XAUUSDcent,
    XAUUSDPRO, #XAUUSD, m.XAUUSD, XAU_USD, XAU/USD, GOLD and Gold.m are all XAUUSD.
    Names with spaces ("Gold Futures") are descriptions of other products, so they are left whole.
    """
    name = symbol.strip().replace("/", "")
    if " " in name:
        return name.upper()

    name = re.sub(r"^([A-Za-z]{3})[.\-_]([A-Za-z]{3})(?![A-Za-z0-9])", r"\1\2", name)  # XAU_USD, EUR.USD
    name = re.sub(r"^(?:[^A-Za-z0-9]+|[A-Za-z]{1,3}[.\-_])", "", name)                  # #XAUUSD, m.XAUUSD
    core = re.split(r"[^A-Za-z0-9]", name, maxsplit=1)[0]                               # XAUUSD.m, XAUUSD+

    alias = _ALIAS_NAME.fullmatch(core)
    if alias:
        return _SYMBOL_ALIASES[alias.group(1).upper()]

    if not core.islower():
        core = re.sub(r"[a-z]+$", "", core)  # XAUUSDm, XAUUSDc, XAUUSDcent
    core = core.upper()
    # XAUUSDPRO is XAUUSD, but BTCUSDT is a different instrument from BTCUSD: its quote currency is USDT
    if 6 < len(core) <= 9 and not core.endswith(("USDT", "USDC")):
        core = core[:6]
    return core or name.upper()


class Settings(BaseSettings):
    # MT5 Connection
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_terminal_path: str = ""
    dry_run: bool = False                 # Set to True to simulate trades without real MT5 connection

    # API Security
    api_key: str = ""

    # Trading
    allowed_symbols: str | list[str] = ["XAUUSD", "BTCUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY"]
    default_volume: float = 0.01          # default trade volume in lots
    cent_account: str = "auto"            # auto reads the account currency (USC...); true/false for brokers that show cents as USD
    eurusd_volume: float = 0.01           # lot size for EURUSD
    gbpusd_volume: float = 0.01           # lot size for GBPUSD
    usdjpy_volume: float = 0.01           # lot size for USDJPY
    audusd_volume: float = 0.01           # lot size for AUDUSD
    btcusd_volume: float = 0.01           # lot size for BTCUSD
    xagusd_volume: float = 0.01           # lot size for XAGUSD
    xauusd_volume: float = 0.01          # legacy, keeping for compatibility
    magic_number: int = 10001
    default_deviation: int = 20
    default_comment: str = "SignalTrade Auto"
    max_positions_per_symbol: int = 2
    max_buy_positions_per_symbol: int = 1
    max_sell_positions_per_symbol: int = 1
    tp_reduction_pct: float = 0.10         # reduce TP by X% of entry→TP distance (spread buffer)
    sl_reduction_pct: float = 0.0          # pull SL X% closer to entry. Not a spread buffer: a BUY's SL triggers on the bid, so
                                           # spread already brings it closer; pulling it in more only adds stop-outs.
                                           # Replay (Oct 2026, gold): signal SL as sent beat 10% closer (+$52 vs +$40)
    # SignalTrade integration
    signaltrade_url: str = "http://localhost:3000"
    signaltrade_poll_interval: float = 1.0   # seconds between each poll
    max_signal_age_minutes: float = 10.0     # ignore signals published longer ago (a still-open trade is re-served with its old timestamp)

    # Trade monitor (adaptive SL/TP)
    monitor_poll_interval: float = 1.0  # seconds between each monitor cycle
    modify_min_interval_s: float = 3.0  # min seconds between SL/TP modify requests per position (broker rate limit)
    open_reject_cooldown_s: float = 300.0  # after the broker rejects an open, don't try that pair again for this long
    monitor_tp1_proximity: float = 5.0  # how close price must be to TP1 to trigger Phase 2 (in pips, e.g. 5.0)

    # Adaptive SL/TP Customization
    early_risk_reduction_enabled: bool = True
    early_risk_cut_trigger_pct: float = 0.20   # 20% of TP1 move -> cut SL risk by 50%
    early_step_25_trigger_pct: float = 0.25    # 25% of TP1 move -> lock 2% of TP1 profit
    early_step_25_lock_pct: float = 0.02       # lock 2% of TP1 profit distance
    early_step_30_trigger_pct: float = 0.30    # 30% of TP1 move -> lock 5% of TP1 profit
    early_step_30_lock_pct: float = 0.05       # lock 5% of TP1 profit distance
    early_step_40_trigger_pct: float = 0.40    # 40% of TP1 move -> lock 10% of TP1 profit
    early_step_40_lock_pct: float = 0.10       # lock 10% of TP1 profit distance
    early_step_50_trigger_pct: float = 0.50    # 50% of TP1 move -> lock 25% of TP1 profit
    early_step_50_lock_pct: float = 0.25       # lock 25% of TP1 profit distance
    phase1_trigger_pct: float = 0.60       # 60% of TP1 move -> trigger Phase 1 lock
    phase1_lock_pct: float = 0.50          # percentage of TP1 distance locked as profit at Phase 1 trigger
    phase2_lock_pct: float = 0.70          # percentage of TP1 distance locked as profit when TP1 is hit
    trailing_sl_pct: float = 0.20          # trailing SL distance as percentage of entry-to-TP2 total move

    # Noise floor: every SL the ladder sets stays at least k × ATR(M1) away from the current price (0 disables).
    # The % steps above put the stop ~$0.6 from price after a $0.7 move, inside gold's normal 1-minute swings
    # (ATR ~$1.4), so trades that later ran $9-15 were stopped at +$0.05. Replay (179 gold signals, Oct 7-20 2026):
    # k=0.6 matched or beat the current ladder in every half of the data; all signals +$40.21 vs +$17.27,
    # bot rules +$48.38 vs +$33.39. The floor never widens a stop: the SL still only ever moves toward profit.
    noise_floor_atr_mult: float = 0.6
    noise_floor_atr_period: int = 14       # completed M1 bars in the ATR

    # Trading Session Restrictions
    session_restrictions_enabled: bool = True
    allowed_sessions: str | list[str] = ["london", "asia"]
    avoid_sessions: str | list[str] = ["us"]

    # Local Time Restrictions
    local_time_restriction_enabled: bool = True
    local_time_start: str = "10:00"
    local_time_end: str = "20:00"

    # XAUUSD Specific Volumes & Days
    xauusd_weekday_volume: float = 0.10
    xauusd_friday_volume: float = 0.01
    xauusd_allowed_weekdays: str | list[str] = ["monday", "tuesday", "wednesday"]

    # Daily Profit Target Circuit Breaker
    daily_profit_target_enabled: bool = True
    daily_profit_target_usd: float = 50.0   # Stop trading if profit reaches $50 USD
    daily_profit_target_pct: float = 0.05   # Stop trading if profit reaches 5% of day's start balance

    # Dynamic Daily Drawdown Circuit Breaker
    daily_drawdown_enabled: bool = True
    daily_drawdown_pct: float = 0.10        # 10% of day's peak/starting balance
    daily_drawdown_min_usd: float = 100.0   # $100 USD minimum threshold

    # Minimum Remaining TP Distance Filter (Risk:Reward Guard)
    min_remaining_tp_enabled: bool = True
    min_remaining_tp_pct: float = 0.70      # Require at least 70% of TP1 distance remaining from current price

    # Re-anchor SL/TP to the broker's price at send time, keeping the signal's distances.
    # SignalTrade prices come from a different feed (Deriv) and can sit $1-3 away from the broker on gold, which
    # silently shrank or stretched the real SL/TP. Replay (Oct 2026, 185 gold signals): +$13.81 anchored vs +$0.92.
    anchor_levels_to_market: bool = True
    max_entry_gap_sl_pct: float = 1.0       # skip the signal if broker price is more than this × SL distance from its entry

    # Only trade signals generated in the last N seconds (age from SignalTrade's own timestamp).
    # SignalTrade can serve cached signals up to ~30 min old; live fills so far used signals 0-13 s old.
    max_signal_age_s: float = 30.0

    # News guard: a few minutes before a high-impact release (CPI, NFP, rate decisions) in a currency the position trades
    # on, close the bot's positions there that are in profit; losing ones keep their stop loss (news_guard.py)
    news_close_enabled: bool = True
    news_close_minutes_before: float = 5.0

    # Logging
    log_file: str = "trading.log"
    events_file: str = "logs/events.jsonl"   # structured one-JSON-per-line event log for analysis

    @field_validator("allowed_symbols", mode="after")
    @classmethod
    def canonicalise_allowed_symbols(cls, v: Any) -> Any:
        # The rest of the bot compares base names, so XAUUSD.m in .env must still mean XAUUSD
        return [canonical_symbol(s) for s in v]

    @field_validator("allowed_symbols", "allowed_sessions", "avoid_sessions", "xauusd_allowed_weekdays", mode="before")
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


_cached_settings: Settings | None = None
_cached_env_mtime: float = 0.0


def get_settings() -> Settings:
    global _cached_settings, _cached_env_mtime
    import os
    try:
        mtime = os.path.getmtime(".env")
    except OSError:
        mtime = 0.0
    if _cached_settings is None or mtime != _cached_env_mtime:
        _cached_settings = Settings()
        _cached_env_mtime = mtime
    return _cached_settings
