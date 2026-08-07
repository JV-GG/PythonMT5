"""
Pydantic models for request/response validation.
"""
from dataclasses import dataclass
from pydantic import BaseModel, Field, field_validator, model_validator
from config import get_settings


# ── Trade info dataclass ────────────────────────────────────────────────────────
# Single source of truth for all trade metadata used by the adaptive SL/TP
# managers. Lives here so both the threaded manager (main.py) and the async
# monitor (trade_monitor.py) share the same type without circular imports.

PHASE_INITIAL = "initial"
PHASE_PARTIAL_LOCK = "partial_lock"
PHASE_TP1_HIT = "tp1_hit"


@dataclass
class TradeInfo:
    order_id: int
    symbol: str
    direction: str            # "buy" or "sell"
    entry_price: float
    initial_sl: float
    initial_tp1: float
    tp2: float | None = None
    phase: str = PHASE_INITIAL
    current_sl: float = 0.0
    current_tp: float = 0.0
    triggered_at: float = 0.0


class TradeRequest(BaseModel):
    symbol: str = Field(..., min_length=1, description="Trading symbol, e.g. GBPUSD")
    volume: float | None = Field(default=None, description="Trade volume (lots). If omitted, uses symbol volume from .env")
    order_type: str = Field(..., description="Order type: 'buy' or 'sell'")
    sl: float = Field(..., description="Stop Loss price")
    tp: float = Field(..., description="Take Profit price (TP1 / initial TP)")
    tp1: float | None = Field(default=None, description="First take profit target (TP1)")
    tp_final: float | None = Field(default=None, description="Final take profit target (TP2 / TP Final)")
    comment: str | None = Field(default=None, description="Comment to be sent with trade request")

    @model_validator(mode="after")
    def resolve_volume(self) -> "TradeRequest":
        # Always enforce symbol lot size from .env configuration
        settings = get_settings()
        sym_clean = self.symbol.upper().replace("/", "").strip()

        if sym_clean == "XAUUSD":
            from datetime import datetime, timezone as dt_timezone, timedelta as dt_timedelta
            malaysia_tz = dt_timezone(dt_timedelta(hours=8))
            malaysia_now = datetime.now(malaysia_tz)
            if malaysia_now.weekday() == 4:  # Friday
                self.volume = settings.xauusd_friday_volume
            else:
                self.volume = settings.xauusd_weekday_volume
        else:
            symbol_vol_attr = f"{sym_clean.lower()}_volume"
            if hasattr(settings, symbol_vol_attr):
                self.volume = getattr(settings, symbol_vol_attr)
            else:
                self.volume = settings.default_volume
        return self

    @field_validator("order_type")
    @classmethod
    def validate_order_type(cls, v: str) -> str:
        normalized = v.lower().strip()
        if normalized not in ("buy", "sell"):
            raise ValueError("order_type must be 'buy' or 'sell'")
        return normalized

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        settings = get_settings()
        normalized = v.strip().upper()
        if normalized not in settings.allowed_symbols:
            raise ValueError(
                f"Symbol '{v}' not allowed. Supported: {settings.allowed_symbols}"
            )
        return normalized

    @model_validator(mode="after")
    def validate_tp_direction(self) -> "TradeRequest":
        """
        Reject trades where TP is on the wrong side of entry relative to direction.

        Since market price isn't available at Pydantic parse time, the SL is used
        as a direction proxy: for a BUY, TP > SL; for a SELL, TP < SL.
        This guarantees the TP is in the profit direction and not guaranteed loss.
        """
        if self.order_type == "sell":
            if self.tp >= self.sl:
                raise ValueError(
                    f"Take Profit ({self.tp}) must be below Stop Loss ({self.sl}) "
                    f"for a SELL order. The TP is currently in the loss direction."
                )
        else:
            if self.tp <= self.sl:
                raise ValueError(
                    f"Take Profit ({self.tp}) must be above Stop Loss ({self.sl}) "
                    f"for a BUY order. The TP is currently in the loss direction."
                )
        return self


class TradeResponse(BaseModel):
    success: bool
    order_id: int | None = None
    executed_price: float | None = None
    message: str | None = None


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
