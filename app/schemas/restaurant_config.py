from typing import Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator


class OpeningPeriod(BaseModel):
    weekday: int = Field(ge=0, le=6)
    opens_minute: int = Field(ge=0, le=1439)
    closes_minute: int = Field(ge=1, le=1440)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.opens_minute >= self.closes_minute:
            raise ValueError("Opening time must be before closing time")
        return self


class RestaurantConfiguration(BaseModel):
    restaurant_id: UUID
    restaurant_name: str
    location_id: UUID
    location_name: str
    timezone: str
    currency: Literal["USD"]
    preparation_minutes: int = Field(ge=1, le=180)
    ordering_enabled: bool
    hours: list[OpeningPeriod] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_hours(self) -> Self:
        periods = sorted(self.hours, key=lambda p: (p.weekday, p.opens_minute))
        for previous, current in zip(periods, periods[1:]):
            if (
                previous.weekday == current.weekday
                and current.opens_minute < previous.closes_minute
            ):
                raise ValueError("Opening periods must not overlap")
        return self
