from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictBool

from app.schemas.menu import MenuResponse


class IntakeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Require actual JSON true/false rather than "true" or 1.
    ordering_enabled: StrictBool


class AvailabilityChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_available: StrictBool


class KitchenOverview(BaseModel):
    ordering_enabled: bool
    kitchen_online: bool
    accepting_orders: bool
    block_reason: str | None
    kitchen_last_seen_at: AwareDatetime | None
    menu: MenuResponse