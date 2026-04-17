"""
Pydantic models for request/response validation.
"""
from pydantic import BaseModel, Field, field_validator
from config import get_settings


class TradeRequest(BaseModel):
    symbol: str = Field(..., min_length=1, description="Trading symbol, e.g. BTCUSD")
    volume: float = Field(..., gt=0, description="Trade volume (lots)")
    order_type: str = Field(..., description="Order type: 'buy' or 'sell'")
    sl: float = Field(..., description="Stop Loss price")
    tp: float = Field(..., description="Take Profit price (TP1 / initial TP)")
    tp1: float | None = Field(default=None, description="First take profit target (TP1)")
    tp_final: float | None = Field(default=None, description="Final take profit target (TP2 / TP Final)")

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


class TradeResponse(BaseModel):
    success: bool
    order_id: int | None = None
    executed_price: float | None = None
    message: str | None = None


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
