from uuid import UUID

import asyncpg

from app.schemas.restaurant import Restaurant


class RestaurantRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def create(self, name: str) -> Restaurant:
        row = await self.connection.fetchrow(
            """
            INSERT INTO public.restaurants (name)
            VALUES ($1)
            RETURNING id, name, created_at
            """,
            name,
        )
        if row is None:
            raise RuntimeError("Restaurant insert returned no row")
        return Restaurant.model_validate(dict(row))

    async def get_by_id(self, restaurant_id: UUID) -> Restaurant | None:
        row = await self.connection.fetchrow(
            """
            SELECT id, name, created_at
            FROM public.restaurants
            WHERE id = $1
            """,
            restaurant_id,
        )
        if row is None:
            return None
        return Restaurant.model_validate(dict(row))
