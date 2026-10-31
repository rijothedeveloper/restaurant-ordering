from uuid import UUID

import asyncpg

from app.schemas.menu import (
    MenuCategory,
    MenuItem,
    MenuResponse,
    ModifierGroup,
    ModifierOption,
)


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
                i.is_available,
                g.id AS group_id,
                g.name AS group_name,
                g.min_selections,
                g.max_selections,
                o.id AS option_id,
                o.name AS option_name,
                o.price_delta_cents,
                o.is_available AS option_is_available
            FROM public.restaurant_settings AS s
            LEFT JOIN public.menu_categories AS c ON true
            LEFT JOIN public.menu_items AS i ON i.category_id = c.id
            LEFT JOIN public.modifier_groups AS g ON g.menu_item_id = i.id
            LEFT JOIN public.modifier_options AS o ON o.group_id = g.id
            WHERE s.id = 1
            ORDER BY c.sort_order, c.id, i.sort_order, i.id,
                     g.sort_order, g.id, o.sort_order, o.id
            """
        )
        if not rows:
            return None

        categories: dict[UUID, MenuCategory] = {}
        items: dict[UUID, MenuItem] = {}
        groups: dict[UUID, ModifierGroup] = {}

        for row in rows:
            category_id = row["category_id"]
            if category_id is None:
                continue

            if category_id not in categories:
                categories[category_id] = MenuCategory(
                    id=category_id,
                    name=row["category_name"],
                )

            item_id = row["item_id"]
            if item_id is None:
                continue

            if item_id not in items:
                item = MenuItem(
                    id=item_id,
                    name=row["item_name"],
                    description=row["description"],
                    price_cents=row["price_cents"],
                    is_available=row["is_available"],
                )
                items[item_id] = item
                categories[category_id].items.append(item)

            group_id = row["group_id"]
            if group_id is None:
                continue

            if group_id not in groups:
                group = ModifierGroup(
                    id=group_id,
                    name=row["group_name"],
                    min_selections=row["min_selections"],
                    max_selections=row["max_selections"],
                )
                groups[group_id] = group
                items[item_id].modifier_groups.append(group)

            if row["option_id"] is not None:
                groups[group_id].options.append(
                    ModifierOption(
                        id=row["option_id"],
                        name=row["option_name"],
                        price_delta_cents=row["price_delta_cents"],
                        is_available=row["option_is_available"],
                    )
                )

        return MenuResponse(
            restaurant_name=rows[0]["restaurant_name"],
            ordering_enabled=rows[0]["ordering_enabled"],
            categories=list(categories.values()),
        )
