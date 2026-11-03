from uuid import UUID

import asyncpg


class CartRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def get_status_for_update(
        self, cart_id: UUID, customer_id: UUID
    ) -> str | None:
        return await self.connection.fetchval(
            """
            SELECT status
            FROM public.carts
            WHERE id = $1 AND customer_id = $2
            FOR UPDATE
            """,
            cart_id,
            customer_id,
        )

    async def insert_line(
        self,
        cart_id: UUID,
        item_id: UUID,
        quantity: int,
        option_ids: list[UUID],
    ) -> UUID:
        line_id = await self.connection.fetchval(
            """
            INSERT INTO public.cart_items (cart_id, menu_item_id, quantity)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            cart_id,
            item_id,
            quantity,
        )
        if option_ids:
            await self.connection.executemany(
                """
                INSERT INTO public.cart_item_modifiers
                    (cart_item_id, modifier_option_id)
                VALUES ($1, $2)
                """,
                [(line_id, option_id) for option_id in option_ids],
            )
        return line_id
