import json
from uuid import UUID

import asyncpg

from app.repositories.order import OrderRepository
from app.schemas.order import OrderResponse, StaffOrderSummary


class StaffOrderRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def list_active(self) -> list[StaffOrderSummary]:
        rows = await self.connection.fetch(
            """
            SELECT id AS order_id, order_number, status, payment_status,
                   currency, total_cents, placed_at, pickup_at
            FROM public.orders
            WHERE status IN (
                'PLACED', 'ACCEPTED', 'PREPARING', 'READY'
            )
            ORDER BY placed_at, order_number
            """
        )
        return [StaffOrderSummary.model_validate(dict(row)) for row in rows]

    async def get(self, order_id: UUID) -> OrderResponse | None:
        customer_id = await self.connection.fetchval(
            """
            SELECT customer_id
            FROM public.orders
            WHERE id = $1
            """,
            order_id,
        )

        if customer_id is None:
            return None

        # Reuse saved order snapshots without changing customer API ownership.
        return await OrderRepository(self.connection).get(
            order_id,
            customer_id,
        )

    async def lock(self, order_id: UUID) -> asyncpg.Record | None:
        return await self.connection.fetchrow(
            """
            SELECT status, payment_status
            FROM public.orders
            WHERE id = $1
            FOR UPDATE
            """,
            order_id,
        )

    async def set_status(self, order_id: UUID, status: str) -> None:
        # The service holds this order's row lock before calling this method.
        await self.connection.execute(
            """
            UPDATE public.orders
            SET status = $2
            WHERE id = $1
            """,
            order_id,
            status,
        )

    async def set_paid(self, order_id: UUID) -> None:
        await self.connection.execute(
            """
            UPDATE public.orders
            SET payment_status = 'PAID'
            WHERE id = $1
            """,
            order_id,
        )

    async def append_event(
        self,
        order_id: UUID,
        event_type: str,
        data: dict[str, str],
    ) -> None:
        # Record the time after waiting for the lock, not transaction start time.
        await self.connection.execute(
            """
            INSERT INTO public.order_events (
                order_id, event_type, data, occurred_at
            )
            VALUES ($1, $2, $3::jsonb, clock_timestamp())
            """,
            order_id,
            event_type,
            json.dumps(data),
        )
