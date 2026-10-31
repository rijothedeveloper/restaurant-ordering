from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class MenuItem(BaseModel):
    id: UUID
    name: str
    description: str
    price_cents: int = Field(ge=0)
    is_available: bool


class MenuCategory(BaseModel):
    id: UUID
    name: str
    items: list[MenuItem] = Field(default_factory=list)


class MenuResponse(BaseModel):
    restaurant_name: str
    currency: Literal["USD"] = "USD"
    ordering_enabled: bool
    categories: list[MenuCategory] = Field(default_factory=list)
