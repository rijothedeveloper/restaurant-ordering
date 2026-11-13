import asyncpg


class KitchenRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def heartbeat(self) -> bool:
        # Use database time; the browser cannot choose its own expiry.
        updated = await self.connection.fetchval(
            """
            UPDATE public.restaurant_settings
            SET kitchen_last_seen_at = clock_timestamp()
            WHERE id = 1
            RETURNING id
            """
        )
        return updated is not None

    async def set_ordering_enabled(self, enabled: bool) -> bool:
        # Set an explicit value. Retrying "pause" must not toggle ordering on.
        updated = await self.connection.fetchval(
            """
            UPDATE public.restaurant_settings
            SET ordering_enabled = $1
            WHERE id = 1
            RETURNING id
            """,
            enabled,
        )
        return updated is not None