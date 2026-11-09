from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg

from app.repositories.quote import QuoteRepository
from app.repositories.restaurant_config import RestaurantConfigurationRepository
from app.schemas.cart import CartResponse
from app.schemas.quote import QuoteLine, QuoteOption, QuoteResponse
from app.schemas.restaurant_config import RestaurantConfiguration
from app.services.cart import CartError, get_cart
from app.services.opening_hours import is_open_now

QUOTE_TTL_MINUTES = 2


class QuoteError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def calculate_tax(subtotal_cents: int, tax_rate_bps: int) -> int:
    return (subtotal_cents * tax_rate_bps + 5000) // 10000


def build_quote(
    cart: CartResponse,
    config: RestaurantConfiguration,
    now: datetime,
) -> QuoteResponse:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Pass a timezone-aware datetime")

    now = now.astimezone(UTC)

    if cart.status != "BUILDING":
        raise QuoteError("cart_not_quotable", "This cart cannot be quoted.")

    if not cart.items:
        raise QuoteError("empty_cart", "Add an item before requesting a quote.")

    if cart.subtotal_cents is None or any(
        line.issue is not None
        or line.item_name is None
        or line.unit_price_cents is None
        or line.line_total_cents is None
        for line in cart.items
    ):
        raise QuoteError(
            "invalid_cart", "Correct unavailable or invalid cart selections."
        )

    if not config.ordering_enabled:
        raise QuoteError("ordering_paused", "The restaurant is not accepting orders.")

    if config.tax_rate_bps is None:
        raise QuoteError(
            "tax_not_configured", "The restaurant tax rate is not configured."
        )

    if not is_open_now(config, now):
        raise QuoteError("restaurant_closed", "The restaurant is closed.")

    expires_at = now + timedelta(minutes=QUOTE_TTL_MINUTES)
    pickup_at = expires_at + timedelta(minutes=config.preparation_minutes)

    for offset in range(1, QUOTE_TTL_MINUTES + config.preparation_minutes + 1):
        if not is_open_now(config, now + timedelta(minutes=offset)):
            raise QuoteError(
                "pickup_unavailable", "There is not enough open time for pickup."
            )

    items = [
        QuoteLine(
            line_id=line.line_id,
            item_id=line.item_id,
            item_name=line.item_name,
            quantity=line.quantity,
            unit_price_cents=line.unit_price_cents,
            line_total_cents=line.line_total_cents,
            options=[
                QuoteOption(
                    option_id=option.id,
                    name=option.name,
                    price_delta_cents=option.price_delta_cents,
                )
                for option in line.options
            ],
        )
        for line in cart.items
    ]

    subtotal = sum(line.line_total_cents for line in items)
    tax = calculate_tax(subtotal, config.tax_rate_bps)

    return QuoteResponse(
        quote_id=uuid4(),
        cart_id=cart.cart_id,
        cart_version=cart.version,
        items=items,
        subtotal_cents=subtotal,
        tax_rate_bps=config.tax_rate_bps,
        tax_cents=tax,
        total_cents=subtotal + tax,
        created_at=now,
        expires_at=expires_at,
        pickup_at=pickup_at,
    )


async def create_quote(
    connection: asyncpg.Connection,
    *,
    cart_id: UUID,
    customer_id: UUID,
    expected_version: int,
) -> QuoteResponse:
    if type(expected_version) is not int or not 0 <= expected_version < 2**63:
        raise CartError(
            "invalid_cart_version", "Cart version must be a non-negative integer."
        )

    async with connection.transaction():
        cart = await get_cart(
            connection,
            cart_id=cart_id,
            customer_id=customer_id,
        )

        if cart.version != expected_version:
            raise CartError(
                "cart_version_conflict", "Cart changed. Read it again before quoting."
            )

        config = await RestaurantConfigurationRepository(connection).get()

        if config is None:
            raise QuoteError(
                "restaurant_not_configured", "Restaurant is not configured."
            )

        now = await connection.fetchval("SELECT clock_timestamp()")
        quote = build_quote(cart, config, now)
        await QuoteRepository(connection).insert(quote)

    return quote


async def get_quote(
    connection: asyncpg.Connection,
    *,
    quote_id: UUID,
    cart_id: UUID,
    customer_id: UUID,
) -> QuoteResponse:
    saved = await QuoteRepository(connection).get(
        quote_id,
        cart_id,
        customer_id,
    )

    if saved is None:
        raise QuoteError("quote_not_found", "Quote was not found.")

    quote, current_version, checked_at = saved

    if quote.cart_version != current_version:
        raise QuoteError("quote_stale", "The cart changed. Request a new quote.")

    if checked_at >= quote.expires_at:
        raise QuoteError("quote_expired", "This quote expired. Request a new quote.")

    return quote
