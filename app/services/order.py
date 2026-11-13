from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from app.repositories.cart import CartRepository
from app.repositories.order import OrderRepository
from app.repositories.quote import QuoteRepository
from app.repositories.restaurant_config import RestaurantConfigurationRepository
from app.schemas.cart import CartResponse
from app.schemas.order import CheckoutNeedsReview, OrderResponse
from app.schemas.quote import QuoteResponse
from app.schemas.restaurant_config import RestaurantConfiguration
from app.services.cart import CartError, get_cart
from app.services.intake import INTAKE_MESSAGES, intake_block_reason
from app.services.opening_hours import is_open_now
from app.services.quote import (
    QuoteError,
    calculate_tax,
    create_quote,
    get_confirmed_quote,
)


class OrderError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def validate_quote_for_placement(
    quote: QuoteResponse,
    cart: CartResponse,
    config: RestaurantConfiguration,
    now: datetime,
) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Pass a timezone-aware datetime")

    now = now.astimezone(UTC)

    if now >= quote.expires_at:
        raise QuoteError(
            "quote_expired",
            "This quote expired. Request a new quote.",
        )

    if cart.cart_id != quote.cart_id or cart.version != quote.cart_version:
        raise QuoteError(
            "quote_stale",
            "The cart changed. Request a new quote.",
        )

    if cart.status != "BUILDING":
        raise QuoteError(
            "cart_not_quotable",
            "This cart cannot be placed.",
        )

    # The async caller supplies database time.
    # Recheck current intake conditions whenever an offer is built or placed.
    reason = intake_block_reason(config, now)
    if reason is not None:
        raise QuoteError(reason, INTAKE_MESSAGES[reason])

    if cart.subtotal_cents is None or any(line.issue for line in cart.items):
        raise QuoteError(
            "invalid_cart",
            "Correct unavailable or invalid selections.",
        )

    changed = QuoteError(
        "quote_changed",
        "Review and confirm a new quote.",
    )

    quoted = {line.line_id: line for line in quote.items}

    if len(cart.items) != len(quoted):
        raise changed

    for current in cart.items:
        old = quoted.get(current.line_id)

        if old is None or (
            current.item_id,
            current.item_name,
            current.quantity,
            current.unit_price_cents,
            current.line_total_cents,
        ) != (
            old.item_id,
            old.item_name,
            old.quantity,
            old.unit_price_cents,
            old.line_total_cents,
        ):
            raise changed

        current_options = {
            (option.id, option.name, option.price_delta_cents)
            for option in current.options
        }
        quoted_options = {
            (option.option_id, option.name, option.price_delta_cents)
            for option in old.options
        }

        if current_options != quoted_options:
            raise changed

    subtotal = cart.subtotal_cents
    tax = calculate_tax(subtotal, config.tax_rate_bps)

    if (
        quote.subtotal_cents,
        quote.tax_rate_bps,
        quote.tax_cents,
        quote.total_cents,
    ) != (
        subtotal,
        config.tax_rate_bps,
        tax,
        subtotal + tax,
    ):
        raise changed

    pickup_at = quote.pickup_at.astimezone(UTC)

    if now + timedelta(minutes=config.preparation_minutes) > pickup_at:
        raise changed

    # Keep the promised pickup time.
    # Check the intervening minutes and the pickup endpoint.
    cursor = now
    while cursor < pickup_at:
        if not is_open_now(config, cursor):
            raise changed
        cursor += timedelta(minutes=1)

    if not is_open_now(config, pickup_at):
        raise changed


async def place_order(
    connection: asyncpg.Connection,
    *,
    quote_id: UUID,
    cart_id: UUID,
    customer_id: UUID,
) -> OrderResponse | CheckoutNeedsReview:
    async with connection.transaction(isolation="read_committed"):
        carts = CartRepository(connection)

        status = await carts.get_status_for_update(cart_id, customer_id)
        if status is None:
            raise CartError("cart_not_found", "Cart was not found.")

        repository = OrderRepository(connection)
        existing = await repository.get_for_cart(cart_id, customer_id)

        if existing is not None:
            if existing.quote_id != quote_id:
                raise OrderError(
                    "order_quote_mismatch",
                    "This cart was placed with another quote.",
                )

            result = existing
        else:
            # Cart -> catalog -> quote.
            # Read the catalog only after locking it.
            await repository.lock_catalog()

            quote, confirmed_at = await get_confirmed_quote(
                connection,
                quote_id=quote_id,
                cart_id=cart_id,
                customer_id=customer_id,
            )

            config = await RestaurantConfigurationRepository(connection).get()

            try:
                if config is None:
                    raise QuoteError(
                        "restaurant_not_configured",
                        "Restaurant is not configured.",
                    )

                cart = await get_cart(
                    connection,
                    cart_id=cart_id,
                    customer_id=customer_id,
                )
                placed_at = await connection.fetchval("SELECT clock_timestamp()")

                validate_quote_for_placement(
                    quote,
                    cart,
                    config,
                    placed_at,
                )

            except QuoteError as error:
                # Commit invalidation; raising here would roll it back.
                await QuoteRepository(connection).clear_pending(cart_id)

                replacement = None
                if error.code == "quote_changed":
                    try:
                        replacement = await create_quote(
                            connection,
                            cart_id=cart_id,
                            customer_id=customer_id,
                            expected_version=quote.cart_version,
                        )
                    except QuoteError as refresh_error:
                        error = refresh_error

                result = CheckoutNeedsReview(
                    code=error.code,
                    message=str(error),
                    quote=replacement,
                )

            else:
                order_id = await repository.insert(
                    quote,
                    customer_id,
                    confirmed_at,
                    placed_at,
                )
                await repository.insert_items(order_id, quote.items)

                if not await carts.mark_checked_out(
                    cart_id,
                    quote.cart_version,
                ):
                    raise OrderError(
                        "cart_checkout_conflict",
                        "Cart could not be checked out.",
                    )

                await QuoteRepository(connection).clear_pending(cart_id)
                await repository.append_placed_event(order_id, placed_at)

                result = await repository.get(order_id, customer_id)
                if result is None:
                    raise RuntimeError("The inserted order could not be read")

    return result


async def get_order(
    connection: asyncpg.Connection,
    *,
    order_id: UUID,
    customer_id: UUID,
) -> OrderResponse:
    result = await OrderRepository(connection).get(order_id, customer_id)

    if result is None:
        raise OrderError("order_not_found", "Order was not found.")

    return result
