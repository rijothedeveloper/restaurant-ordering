import json
from datetime import datetime
from uuid import UUID

import asyncpg

from app.schemas.quote import QuoteResponse


class QuoteRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def insert(self, quote: QuoteResponse) -> None:
        await self.connection.execute(
            """
            INSERT INTO public.quotes (
                id, cart_id, cart_version, currency, items,
                subtotal_cents, tax_rate_bps, tax_cents, total_cents,
                created_at, expires_at, pickup_at
            ) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12)
            """,
            quote.quote_id,
            quote.cart_id,
            quote.cart_version,
            quote.currency,
            json.dumps([line.model_dump(mode="json") for line in quote.items]),
            quote.subtotal_cents,
            quote.tax_rate_bps,
            quote.tax_cents,
            quote.total_cents,
            quote.created_at,
            quote.expires_at,
            quote.pickup_at,
        )

    async def get(
        self, quote_id: UUID, cart_id: UUID, customer_id: UUID
    ) -> tuple[QuoteResponse, int, datetime] | None:
        row = await self.connection.fetchrow(
            """
            SELECT q.id AS quote_id, q.cart_id, q.cart_version, q.currency,
                   q.items::text AS items, q.subtotal_cents, q.tax_rate_bps,
                   q.tax_cents, q.total_cents, q.created_at, q.expires_at, q.pickup_at,
                   c.version AS current_version, clock_timestamp() AS checked_at
            FROM public.quotes AS q
            JOIN public.carts AS c ON c.id = q.cart_id
            WHERE q.id = $1 AND q.cart_id = $2 AND c.customer_id = $3
            """,
            quote_id,
            cart_id,
            customer_id,
        )

        if row is None:
            return None

        data = dict(row)
        data["items"] = json.loads(data["items"])
        current_version = data.pop("current_version")
        checked_at = data.pop("checked_at")

        return QuoteResponse.model_validate(data), current_version, checked_at

    async def clear_pending(self, cart_id: UUID) -> None:
        await self.connection.execute(
            """
            UPDATE public.quotes SET is_pending = false
            WHERE cart_id = $1 AND is_pending
            """,
            cart_id,
        )

    async def make_pending(self, quote_id: UUID, cart_id: UUID) -> None:
        # Caller must hold the cart lock inside a transaction.
        await self.clear_pending(cart_id)

        activated = await self.connection.fetchval(
            """
            UPDATE public.quotes SET is_pending = true
            WHERE id = $1 AND cart_id = $2
            RETURNING id
            """,
            quote_id,
            cart_id,
        )
        if activated is None:
            raise RuntimeError("Cannot activate a missing quote")

    async def get_confirmation_state_for_update(
        self, quote_id: UUID, cart_id: UUID
    ) -> asyncpg.Record | None:
        return await self.connection.fetchrow(
            """
            SELECT cart_version, expires_at, is_pending, readback_token,
                   readback_completed_at, confirmed_at
            FROM public.quotes
            WHERE id = $1 AND cart_id = $2
            FOR UPDATE
            """,
            quote_id,
            cart_id,
        )

    async def record_readback(
        self, quote_id: UUID, token: UUID, completed_at: datetime
    ) -> None:
        await self.connection.execute(
            """
            UPDATE public.quotes
            SET readback_token = $2, readback_completed_at = $3,
                confirmed_at = NULL
            WHERE id = $1
            """,
            quote_id,
            token,
            completed_at,
        )

    async def record_confirmation(
        self, quote_id: UUID, confirmed_at: datetime
    ) -> datetime:
        return await self.connection.fetchval(
            """
            UPDATE public.quotes
            SET confirmed_at = COALESCE(confirmed_at, $2)
            WHERE id = $1
            RETURNING confirmed_at
            """,
            quote_id,
            confirmed_at,
        )
