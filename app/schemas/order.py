from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from app.schemas.quote import QuoteLine, QuoteResponse


class OrderResponse(BaseModel):
    order_id: UUID
    order_number: int = Field(gt=0)
    cart_id: UUID
    quote_id: UUID
    cart_version: int = Field(ge=0)

    status: Literal[
        "PLACED",
        "ACCEPTED",
        "PREPARING",
        "READY",
        "COMPLETED",
        "REJECTED",
        "CANCELLED",
    ]
    payment_status: Literal["UNPAID", "PAID"]
    currency: Literal["USD"]

    items: list[QuoteLine] = Field(min_length=1)
    subtotal_cents: int = Field(ge=0)
    tax_rate_bps: int = Field(ge=0, le=10000)
    tax_cents: int = Field(ge=0)
    total_cents: int = Field(ge=0)

    confirmed_at: AwareDatetime
    placed_at: AwareDatetime
    pickup_at: AwareDatetime


class CheckoutNeedsReview(BaseModel):
    code: str
    message: str
    quote: QuoteResponse | None = None
