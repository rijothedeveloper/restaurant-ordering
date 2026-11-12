from typing import List
from uuid import UUID

import asyncpg

from app.repositories.order import OrderRepository
from app.schemas.order import OrderResponse, StaffOrderSummary


class StaffOrderRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def list_active(self) -> List[StaffOrderSummary]:
        rows = await self.connection.fetch(
            """
            SELECT id AS order_id, order_number, status, payment_status,
                   currency, total_cents, placed_at, pickup_at
            FROM public.orders
            WHERE status IN ('PLACED', 'ACCEPTED', 'PREPARING', 'READY')
            ORDER BY placed_at, order_number
            """
        )

        return [StaffOrderSummary.model_validate(dict(row)) for row in rows]

    async def get(self, order_id: UUID) -> OrderResponse | None:
        # Staff may read any order in this single-restaurant application.
        # Customer ownership checks in OrderRepository remain unchanged.
        customer_id = await self.connection.fetchval(
            "SELECT customer_id FROM public.orders WHERE id = $1",
            order_id,
        )

        if customer_id is None:
            return None

        return await OrderRepository(self.connection).get(
            order_id,
            customer_id,
        )
