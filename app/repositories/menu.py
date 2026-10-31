from uuid import UUID

import asyncpg

from app.schemas.menu import MenuCategory, MenuItem, MenuResponse


class MenuRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def get(self) -> MenuResponse | None:
        rows = await self.connection.fetch(
            """
            SELECT
                s.name AS restaurant_name,
                s.ordering_enabled,
                c.id AS category_id,
                c.name AS category_name,
                i.id AS item_id,
                i.name AS item_name,
                i.description,
                i.price_cents,
                i.is_available
            FROM public.restaurant_settings AS s
            LEFT JOIN public.menu_categories AS c ON true
            LEFT JOIN public.menu_items AS i ON i.category_id = c.id
            WHERE s.id = 1
            ORDER BY c.sort_order, c.id, i.sort_order, i.id
            """
        )
        if not rows:
            return None

        categories: dict[UUID, MenuCategory] = {}

        for row in rows:
            category_id = row["category_id"]

            if category_id is None:
                continue

            if category_id not in categories:
                categories[category_id] = MenuCategory(
                    id=category_id,
                    name=row["category_name"],
                )

            if row["item_id"] is not None:
                categories[category_id].items.append(
                    MenuItem(
                        id=row["item_id"],
                        name=row["item_name"],
                        description=row["description"],
                        price_cents=row["price_cents"],
                        is_available=row["is_available"],
                    )
                )

        return MenuResponse(
            restaurant_name=rows[0]["restaurant_name"],
            ordering_enabled=rows[0]["ordering_enabled"],
            categories=list(categories.values()),
        )
