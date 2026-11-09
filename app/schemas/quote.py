from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field


class QuoteOption(BaseModel):
    option_id: UUID
    name: str
    price_delta_cents: int = Field(ge=0)


class QuoteLine(BaseModel):
    line_id: UUID
    item_id: UUID
    item_name: str
    quantity: int = Field(ge=1, le=20)
    options: list[QuoteOption]
    unit_price_cents: int = Field(ge=0)
    line_total_cents: int = Field(ge=0)


class QuoteResponse(BaseModel):
    quote_id: UUID
    cart_id: UUID
    cart_version: int = Field(ge=0)
    currency: Literal["USD"] = "USD"
    items: list[QuoteLine] = Field(min_length=1)
    subtotal_cents: int = Field(ge=0)
    tax_rate_bps: int = Field(ge=0, le=10000)
    tax_cents: int = Field(ge=0)
    total_cents: int = Field(ge=0)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    pickup_at: AwareDatetime
