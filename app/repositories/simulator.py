from uuid import UUID

import asyncpg


class SimulatorConfigurationError(ValueError):
    pass


class SimulatorRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def create_context(
        self, *, expected_database: str = "restaurant_dev"
    ) -> tuple[UUID, UUID]:
        # Check the actual database before inserting any development data.
        database = await self.connection.fetchval("SELECT current_database()")
        if database != expected_database:
            raise SimulatorConfigurationError(
                f"Simulator requires the {expected_database} database."
            )

        # Both records commit together; a cart failure must not leave an orphan.
        async with self.connection.transaction():
            customer_id = await self.connection.fetchval(
                "INSERT INTO public.customers DEFAULT VALUES RETURNING id"
            )
            cart_id = await self.connection.fetchval(
                "INSERT INTO public.carts (customer_id) VALUES ($1) RETURNING id",
                customer_id,
            )

        return customer_id, cart_id