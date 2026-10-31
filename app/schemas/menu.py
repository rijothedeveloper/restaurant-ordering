from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ModifierOption(BaseModel):
    id: UUID
    name: str
    price_delta_cents: int = Field(ge=0)
    is_available: bool


class ModifierGroup(BaseModel):
    id: UUID
    name: str
    min_selections: int = Field(ge=0)
    max_selections: int = Field(ge=1)
    options: list[ModifierOption] = Field(default_factory=list)


class MenuItem(BaseModel):
    id: UUID
    name: str
    description: str
    price_cents: int = Field(ge=0)
    is_available: bool
    modifier_groups: list[ModifierGroup] = Field(default_factory=list)


class MenuCategory(BaseModel):
    id: UUID
    name: str
    items: list[MenuItem] = Field(default_factory=list)


class MenuResponse(BaseModel):
    restaurant_name: str
    currency: Literal["USD"] = "USD"
    ordering_enabled: bool
    categories: list[MenuCategory] = Field(default_factory=list)
