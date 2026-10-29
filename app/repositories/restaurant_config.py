import asyncpg

from app.schemas.restaurant_config import OpeningPeriod, RestaurantConfiguration


class RestaurantConfigurationRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def get(self) -> RestaurantConfiguration | None:
        rows = await self.connection.fetch(
            """
            SELECT s.name, s.preparation_minutes, s.ordering_enabled,
                   h.weekday, h.opens_minute, h.closes_minute
            FROM public.restaurant_settings AS s
            LEFT JOIN public.opening_hours AS h ON true
            WHERE s.id = 1
            ORDER BY h.weekday, h.opens_minute
            """
        )
        if not rows:
            return None

        first = rows[0]
        return RestaurantConfiguration(
            restaurant_name=first["name"],
            preparation_minutes=first["preparation_minutes"],
            ordering_enabled=first["ordering_enabled"],
            hours=[
                OpeningPeriod(
                    weekday=row["weekday"],
                    opens_minute=row["opens_minute"],
                    closes_minute=row["closes_minute"],
                )
                for row in rows
                if row["weekday"] is not None
            ],
        )
