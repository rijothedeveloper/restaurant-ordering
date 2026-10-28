from datetime import datetime
from zoneinfo import ZoneInfo

from app.schemas.restaurant_config import RestaurantConfiguration


def is_open_now(config: RestaurantConfiguration, at: datetime) -> bool:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Pass a timezone-aware datetime")

    local = at.astimezone(ZoneInfo(config.timezone))
    minute = local.hour * 60 + local.minute
    return any(
        period.weekday == local.weekday()
        and period.opens_minute <= minute < period.closes_minute
        for period in config.hours
    )
