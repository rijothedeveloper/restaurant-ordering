from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.menu import ModifierOption
from app.schemas.selection import SelectionRequest, ValidatedSelection


class AddCartItemRequest(SelectionRequest):
    quantity: int = Field(default=1, ge=1, le=20, strict=True)


class AddedCartItem(ValidatedSelection):
    line_id: UUID
    quantity: int = Field(ge=1, le=20)
    line_total_cents: int = Field(ge=0)


class UpdateCartItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: int = Field(ge=1, le=20, strict=True)
    option_ids: list[UUID]


class StoredCartLine(AddCartItemRequest):
    line_id: UUID


class CartLineIssue(BaseModel):
    code: str
    message: str


class CartLine(StoredCartLine):
    item_name: str | None = None
    options: list[ModifierOption] = Field(default_factory=list)
    unit_price_cents: int | None = Field(default=None, ge=0)
    line_total_cents: int | None = Field(default=None, ge=0)
    issue: CartLineIssue | None = None


class CartResponse(BaseModel):
    cart_id: UUID
    status: str
    currency: Literal["USD"] = "USD"
    items: list[CartLine]
    subtotal_cents: int | None = Field(ge=0)
