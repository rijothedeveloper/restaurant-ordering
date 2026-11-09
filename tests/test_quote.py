import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest

from app.repositories.cart import CartRepository
from app.repositories.quote import QuoteRepository
from app.schemas.cart import CartLine, CartLineIssue, CartResponse
from app.schemas.menu import ModifierOption
from app.schemas.restaurant_config import OpeningPeriod, RestaurantConfiguration
from app.services.cart import CartError
from app.services.quote import (
    QuoteError,
    build_quote,
    calculate_tax,
    confirm_quote,
    create_quote,
    get_quote,
)

NOW = datetime(2026, 7, 6, 18, tzinfo=UTC)  # Monday, 11:00 in Los Angeles


def sample_cart() -> CartResponse:
    option = ModifierOption(
        id=uuid4(),
        name="Large",
        price_delta_cents=300,
        is_available=True,
    )
    line = CartLine(
        line_id=uuid4(),
        item_id=uuid4(),
        item_name="Bowl",
        quantity=2,
        option_ids=[option.id],
        options=[option],
        unit_price_cents=1699,
        line_total_cents=3398,
    )
    return CartResponse(
        cart_id=uuid4(),
        version=4,
        status="BUILDING",
        items=[line],
        subtotal_cents=3398,
    )


def sample_config() -> RestaurantConfiguration:
    return RestaurantConfiguration(
        restaurant_name="Test Kitchen",
        ordering_enabled=True,
        preparation_minutes=20,
        tax_rate_bps=1000,
        hours=[OpeningPeriod(weekday=0, opens_minute=660, closes_minute=1260)],
    )


@pytest.mark.parametrize(
    "subtotal,rate,expected",
    [
        (3398, 1000, 340),
        (5, 1000, 1),
        (4, 1000, 0),
        (2, 2500, 1),
        (3398, 0, 0),
        (0, 1000, 0),
        (10000, 825, 825),
    ],
)
def test_tax_rounds_in_integer_cents(subtotal: int, rate: int, expected: int) -> None:
    assert calculate_tax(subtotal, rate) == expected


def test_quote_copies_lines_and_calculates_times() -> None:
    cart = sample_cart()
    quote = build_quote(cart, sample_config(), NOW)

    assert quote.cart_id == cart.cart_id
    assert quote.cart_version == 4
    assert (quote.subtotal_cents, quote.tax_cents, quote.total_cents) == (
        3398,
        340,
        3738,
    )
    assert quote.created_at == NOW
    assert quote.expires_at == NOW + timedelta(minutes=2)
    assert quote.pickup_at == NOW + timedelta(minutes=22)

    cart.items[0].options[0].name = "New name"
    assert quote.items[0].options[0].name == "Large"


@pytest.mark.parametrize(
    "case,code",
    [
        ("empty", "empty_cart"),
        ("invalid", "invalid_cart"),
        ("status", "cart_not_quotable"),
        ("paused", "ordering_paused"),
        ("tax", "tax_not_configured"),
        ("closed", "restaurant_closed"),
    ],
)
def test_unquotable_cases(case: str, code: str) -> None:
    cart, config = sample_cart(), sample_config()

    if case == "empty":
        cart.items = []
        cart.subtotal_cents = 0
    elif case == "invalid":
        cart.items[0].issue = CartLineIssue(
            code="option_unavailable", message="Unavailable"
        )
        cart.subtotal_cents = None
    elif case == "status":
        cart.status = "CHECKED_OUT"
    elif case == "paused":
        config.ordering_enabled = False
    elif case == "tax":
        config.tax_rate_bps = None
    else:
        config.hours = []

    with pytest.raises(QuoteError) as error:
        build_quote(cart, config, NOW)

    assert error.value.code == code


@pytest.mark.parametrize("periods", [[(660, 682)], [(660, 665), (680, 720)]])
def test_pickup_excludes_closing_and_intervening_breaks(periods) -> None:
    config = sample_config()
    config.hours = [
        OpeningPeriod(weekday=0, opens_minute=a, closes_minute=b) for a, b in periods
    ]

    with pytest.raises(QuoteError) as error:
        build_quote(sample_cart(), config, NOW)

    assert error.value.code == "pickup_unavailable"


def test_pickup_uses_elapsed_time_through_dst_fallback() -> None:
    config = sample_config()
    config.hours = [OpeningPeriod(weekday=6, opens_minute=60, closes_minute=120)]
    now = datetime(2026, 11, 1, 8, 50, tzinfo=UTC)

    quote = build_quote(sample_cart(), config, now)

    assert quote.pickup_at == datetime(2026, 11, 1, 9, 12, tzinfo=UTC)


def test_quote_refuses_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_quote(sample_cart(), sample_config(), NOW.replace(tzinfo=None))


@pytest.mark.parametrize("bad", [True, "4", -1, 2**63])
def test_quote_creation_requires_valid_expected_version(bad: object) -> None:
    connection = MagicMock(spec=asyncpg.Connection)

    with pytest.raises(CartError) as error:
        asyncio.run(
            create_quote(
                connection,
                cart_id=uuid4(),
                customer_id=uuid4(),
                expected_version=bad,
            )
        )

    assert error.value.code == "invalid_cart_version"
    connection.transaction.assert_not_called()


@pytest.mark.parametrize(
    "offset,version,code",
    [
        (-1, 4, None),
        (0, 4, "quote_expired"),
        (1, 4, "quote_expired"),
        (-1, 5, "quote_stale"),
    ],
)
def test_quote_read_checks_expiry_and_version(monkeypatch, offset, version, code):
    quote = build_quote(sample_cart(), sample_config(), NOW)
    checked_at = quote.expires_at + timedelta(microseconds=offset)

    monkeypatch.setattr(
        QuoteRepository,
        "get",
        AsyncMock(return_value=(quote, version, checked_at)),
    )

    operation = get_quote(
        MagicMock(spec=asyncpg.Connection),
        quote_id=quote.quote_id,
        cart_id=quote.cart_id,
        customer_id=uuid4(),
    )

    if code is None:
        assert asyncio.run(operation) == quote
    else:
        with pytest.raises(QuoteError) as error:
            asyncio.run(operation)
        assert error.value.code == code


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_confirmation_expiry_boundary(monkeypatch, offset):
    quote = build_quote(sample_cart(), sample_config(), NOW)
    token = uuid4()
    checked_at = quote.expires_at + timedelta(microseconds=offset)

    connection = MagicMock(spec=asyncpg.Connection)
    connection.transaction.return_value.__aenter__ = AsyncMock()
    connection.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    connection.fetchval = AsyncMock(return_value=checked_at)

    monkeypatch.setattr(
        CartRepository,
        "get_status_for_update",
        AsyncMock(return_value="BUILDING"),
    )
    monkeypatch.setattr(
        CartRepository,
        "get_version",
        AsyncMock(return_value=4),
    )
    monkeypatch.setattr(
        QuoteRepository,
        "get_confirmation_state_for_update",
        AsyncMock(
            return_value={
                "cart_version": 4,
                "expires_at": quote.expires_at,
                "is_pending": True,
                "readback_token": token,
                "readback_completed_at": NOW,
                "confirmed_at": None,
            }
        ),
    )

    write = AsyncMock(return_value=checked_at)
    monkeypatch.setattr(QuoteRepository, "record_confirmation", write)

    operation = confirm_quote(
        connection,
        quote_id=quote.quote_id,
        cart_id=quote.cart_id,
        customer_id=uuid4(),
        readback_token=token,
        answer="yes",
    )

    if offset < 0:
        assert asyncio.run(operation).confirmed_at == checked_at
    else:
        with pytest.raises(QuoteError) as error:
            asyncio.run(operation)

        assert error.value.code == "quote_expired"
        write.assert_not_awaited()
