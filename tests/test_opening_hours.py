from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.restaurant_config import OpeningPeriod, RestaurantConfiguration
from app.services.opening_hours import is_open_now


def make_config(**changes: object) -> RestaurantConfiguration:
    data = {
        "restaurant_id": uuid4(),
        "restaurant_name": "Test Kitchen",
        "location_id": uuid4(),
        "location_name": "Main",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "preparation_minutes": 20,
        "ordering_enabled": False,
        "hours": [{"weekday": 0, "opens_minute": 660, "closes_minute": 1260}],
    }
    data.update(changes)
    return RestaurantConfiguration.model_validate(data)


@pytest.mark.parametrize(
    "instant, expected",
    [
        ("2026-07-06T17:59:59+00:00", False),
        ("2026-07-06T18:00:00+00:00", True),
        ("2026-07-07T03:59:59+00:00", True),
        ("2026-07-07T04:00:00+00:00", False),
        ("2026-01-05T18:59:59+00:00", False),
        ("2026-01-05T19:00:00+00:00", True),
        ("2026-01-06T05:00:00+00:00", False),
        ("2026-07-07T18:00:00+00:00", False),
    ],
)
def test_local_hours_and_boundaries(instant: str, expected: bool) -> None:
    config = make_config()
    assert is_open_now(config, datetime.fromisoformat(instant)) is expected


@pytest.mark.parametrize(
    "period",
    [
        {"weekday": 7, "opens_minute": 660, "closes_minute": 1260},
        {"weekday": 0, "opens_minute": 900, "closes_minute": 660},
        {"weekday": 0, "opens_minute": 660, "closes_minute": 660},
        {"weekday": 0, "opens_minute": 0, "closes_minute": 1441},
    ],
)
def test_invalid_periods_are_rejected(period: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        OpeningPeriod.model_validate(period)


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "Not/A_Real_Zone"},
        {"currency": "EUR"},
        {"preparation_minutes": 0},
    ],
)
def test_invalid_configuration_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_config(**changes)


def test_overlapping_periods_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must not overlap"):
        make_config(
            hours=[
                {"weekday": 0, "opens_minute": 660, "closes_minute": 900},
                {"weekday": 0, "opens_minute": 899, "closes_minute": 1260},
            ]
        )


def test_adjacent_periods_are_allowed() -> None:
    config = make_config(
        hours=[
            {"weekday": 0, "opens_minute": 660, "closes_minute": 900},
            {"weekday": 0, "opens_minute": 900, "closes_minute": 1260},
        ]
    )
    assert len(config.hours) == 2


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        is_open_now(make_config(), datetime(2026, 7, 6, 12))


@pytest.mark.parametrize(
    "instant, expected",
    [
        ("2026-07-06T07:30:00+00:00", True),
        ("2026-07-06T09:00:00+00:00", False),
    ],
)
def test_overnight_hours_split_at_midnight(instant: str, expected: bool) -> None:
    config = make_config(
        hours=[
            {"weekday": 6, "opens_minute": 1320, "closes_minute": 1440},
            {"weekday": 0, "opens_minute": 0, "closes_minute": 120},
        ]
    )
    assert is_open_now(config, datetime.fromisoformat(instant)) is expected


def test_no_hours_means_closed() -> None:
    assert not is_open_now(
        make_config(hours=[]), datetime.fromisoformat("2026-07-06T18:00:00+00:00")
    )


def test_open_hours_and_ordering_switch_are_separate() -> None:
    config = make_config(ordering_enabled=False)
    assert is_open_now(config, datetime.fromisoformat("2026-07-06T18:00:00+00:00"))
    assert config.ordering_enabled is False


@pytest.mark.parametrize(
    "instant",
    ["2026-11-01T08:30:00+00:00", "2026-11-01T09:30:00+00:00"],
)
def test_both_occurrences_of_repeated_local_hour_follow_schedule(instant: str) -> None:
    config = make_config(
        hours=[{"weekday": 6, "opens_minute": 60, "closes_minute": 120}]
    )
    assert is_open_now(config, datetime.fromisoformat(instant))
