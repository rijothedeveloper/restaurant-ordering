from datetime import UTC, datetime, timedelta
from typing import Literal

from app.schemas.restaurant_config import RestaurantConfiguration
from app.services.opening_hours import is_open_now

HEARTBEAT_TTL = timedelta(seconds=30)

IntakeBlockReason = Literal[
    "ordering_paused",
    "tax_not_configured",
    "restaurant_closed",
    "kitchen_offline",
]

INTAKE_MESSAGES = {
    "ordering_paused": "The restaurant is not accepting orders.",
    "tax_not_configured": "The restaurant tax rate is not configured.",
    "restaurant_closed": "The restaurant is closed.",
    "kitchen_offline": "The kitchen is not connected. Please contact staff.",
}


def kitchen_is_online(
    config: RestaurantConfiguration,
    at: datetime,
) -> bool:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Pass a timezone-aware datetime")

    seen = config.kitchen_last_seen_at
    if seen is None:
        return False

    # Compare elapsed time in UTC, including across daylight-saving changes.
    age = at.astimezone(UTC) - seen.astimezone(UTC)

    # Exactly 30 seconds is expired. Future timestamps also fail closed.
    return timedelta(0) <= age < HEARTBEAT_TTL


def intake_block_reason(
    config: RestaurantConfiguration,
    at: datetime,
) -> IntakeBlockReason | None:
    # Connectivity never overrides a deliberate pause.
    if not config.ordering_enabled:
        return "ordering_paused"

    if config.tax_rate_bps is None:
        return "tax_not_configured"

    if not is_open_now(config, at):
        return "restaurant_closed"

    if not kitchen_is_online(config, at):
        return "kitchen_offline"

    return None