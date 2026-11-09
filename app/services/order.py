from uuid import UUID

import asyncpg

from app.repositories.cart import CartRepository
from app.repositories.order import OrderRepository
from app.repositories.quote import QuoteRepository
from app.schemas.order import OrderResponse
from app.services.quote import QuoteError, get_confirmed_quote


class OrderError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def place_order(
    connection: asyncpg.Connection,
    *,
    quote_id: UUID,
    cart_id: UUID,
    customer_id: UUID,
) -> OrderResponse:
    async with connection.transaction():
        quote, confirmed_at = await get_confirmed_quote(
            connection,
            quote_id=quote_id,
            cart_id=cart_id,
            customer_id=customer_id,
        )

        placed_at = await connection.fetchval("SELECT clock_timestamp()")
        if placed_at >= quote.expires_at:
            raise QuoteError(
                "quote_expired",
                "This quote expired. Request a new quote.",
            )

        repository = OrderRepository(connection)

        order_id = await repository.insert(
            quote,
            customer_id,
            confirmed_at,
            placed_at,
        )
        await repository.insert_items(order_id, quote.items)

        checked_out = await CartRepository(connection).mark_checked_out(
            cart_id,
            quote.cart_version,
        )
        if not checked_out:
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
    result = await OrderRepository(connection).get(
        order_id,
        customer_id,
    )
    if result is None:
        raise OrderError("order_not_found", "Order was not found.")

    return result
