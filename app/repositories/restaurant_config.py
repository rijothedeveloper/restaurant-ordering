from uuid import UUID

import asyncpg

from app.schemas.restaurant_config import OpeningPeriod, RestaurantConfiguration


class RestaurantConfigurationRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def get(
        self, restaurant_id: UUID, location_id: UUID
    ) -> RestaurantConfiguration | None:
        rows = await self.connection.fetch(
            """
            SELECT
                r.id AS restaurant_id,
                r.name AS restaurant_name,
                l.id AS location_id,
                l.name AS location_name,
                l.timezone,
                l.currency,
                l.preparation_minutes,
                l.ordering_enabled,
                h.weekday,
                h.opens_minute,
                h.closes_minute
            FROM public.locations AS l
            JOIN public.restaurants AS r ON r.id = l.restaurant_id
            LEFT JOIN public.location_hours AS h ON h.location_id = l.id
            WHERE r.id = $1 AND l.id = $2
            ORDER BY h.weekday, h.opens_minute
            """,
            restaurant_id,
            location_id,
        )
        if not rows:
            return None

        first = rows[0]
        return RestaurantConfiguration(
            restaurant_id=first["restaurant_id"],
            restaurant_name=first["restaurant_name"],
            location_id=first["location_id"],
            location_name=first["location_name"],
            timezone=first["timezone"],
            currency=first["currency"],
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
