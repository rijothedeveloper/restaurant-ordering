from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.menu import ModifierOption


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: UUID
    option_ids: list[UUID] = Field(default_factory=list)


class ValidatedSelection(BaseModel):
    item_id: UUID
    item_name: str
    options: list[ModifierOption]
    unit_price_cents: int = Field(ge=0)
    currency: Literal["USD"] = "USD"
