from uuid import UUID

import asyncpg

from app.schemas.cart import StoredCartLine


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
        await self._insert_modifiers(line_id, option_ids)
        return line_id

    async def _insert_modifiers(self, line_id: UUID, option_ids: list[UUID]) -> None:
        if option_ids:
            await self.connection.executemany(
                """
                INSERT INTO public.cart_item_modifiers
                    (cart_item_id, modifier_option_id)
                VALUES ($1, $2)
                """,
                [(line_id, option_id) for option_id in option_ids],
            )

    async def list_lines(self, cart_id: UUID) -> list[StoredCartLine]:
        rows = await self.connection.fetch(
            """
            SELECT ci.id AS line_id, ci.menu_item_id AS item_id, ci.quantity,
                COALESCE(
                    array_agg(cim.modifier_option_id ORDER BY cim.modifier_option_id)
                        FILTER (WHERE cim.modifier_option_id IS NOT NULL),
                    ARRAY[]::uuid[]
                ) AS option_ids
            FROM public.cart_items AS ci
            LEFT JOIN public.cart_item_modifiers AS cim ON cim.cart_item_id = ci.id
            WHERE ci.cart_id = $1
            GROUP BY ci.id
            ORDER BY ci.created_at, ci.id
            """,
            cart_id,
        )
        return [StoredCartLine.model_validate(dict(row)) for row in rows]

    async def get_item_id(self, cart_id: UUID, line_id: UUID) -> UUID | None:
        return await self.connection.fetchval(
            """
            SELECT menu_item_id FROM public.cart_items
            WHERE cart_id = $1 AND id = $2
            """,
            cart_id,
            line_id,
        )

    async def replace_line(
        self,
        cart_id: UUID,
        line_id: UUID,
        quantity: int,
        option_ids: list[UUID],
    ) -> bool:
        updated = await self.connection.fetchval(
            """
            UPDATE public.cart_items SET quantity = $3
            WHERE cart_id = $1 AND id = $2
            RETURNING id
            """,
            cart_id,
            line_id,
            quantity,
        )
        if updated is None:
            return False

        await self.connection.execute(
            "DELETE FROM public.cart_item_modifiers WHERE cart_item_id = $1", line_id
        )
        await self._insert_modifiers(line_id, option_ids)
        return True

    async def delete_line(self, cart_id: UUID, line_id: UUID) -> bool:
        deleted = await self.connection.fetchval(
            """
            DELETE FROM public.cart_items
            WHERE cart_id = $1 AND id = $2
            RETURNING id
            """,
            cart_id,
            line_id,
        )
        return deleted is not None

    async def get_version(self, cart_id: UUID) -> int:
        return await self.connection.fetchval(
            "SELECT version FROM public.carts WHERE id = $1", cart_id
        )

    async def advance_version(self, cart_id: UUID, expected_version: int) -> int | None:
        version = await self.connection.fetchval(
            """
            UPDATE public.carts SET version = version + 1
            WHERE id = $1 AND version = $2
            RETURNING version
            """,
            cart_id,
            expected_version,
        )

        if version is not None:
            await self.connection.execute(
                """
                UPDATE public.quotes SET is_pending = false
                WHERE cart_id = $1 AND is_pending
                """,
                cart_id,
            )

        return version

    async def mark_checked_out(self, cart_id: UUID, expected_version: int) -> bool:
        updated = await self.connection.fetchval(
            """
            UPDATE public.carts SET status = 'CHECKED_OUT'
            WHERE id = $1 AND version = $2 AND status = 'BUILDING'
            RETURNING id
            """,
            cart_id,
            expected_version,
        )
        return updated is not None
