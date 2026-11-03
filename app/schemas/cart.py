from uuid import UUID

from pydantic import Field

from app.schemas.selection import SelectionRequest, ValidatedSelection


class AddCartItemRequest(SelectionRequest):
    quantity: int = Field(default=1, ge=1, le=20, strict=True)


class AddedCartItem(ValidatedSelection):
    line_id: UUID
    quantity: int = Field(ge=1, le=20)
    line_total_cents: int = Field(ge=0)
