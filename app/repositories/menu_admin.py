from uuid import UUID

import asyncpg


class MenuAdminRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def update_item(
        self,
        item_id: UUID,
        price_cents: int | None,
        is_available: bool | None,
    ) -> bool:
        updated = await self.connection.fetchval(
            """
            UPDATE public.menu_items
            SET price_cents = COALESCE($2, price_cents),
                is_available = COALESCE($3, is_available)
            WHERE id = $1
            RETURNING id
            """,
            item_id,
            price_cents,
            is_available,
        )

        return updated is not None

    async def update_option(
        self,
        option_id: UUID,
        price_delta_cents: int | None,
        is_available: bool | None,
    ) -> bool:
        updated = await self.connection.fetchval(
            """
            UPDATE public.modifier_options
            SET price_delta_cents = COALESCE($2, price_delta_cents),
                is_available = COALESCE($3, is_available)
            WHERE id = $1
            RETURNING id
            """,
            option_id,
            price_delta_cents,
            is_available,
        )

        return updated is not None
