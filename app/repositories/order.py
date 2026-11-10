import json
from datetime import datetime
from uuid import UUID

import asyncpg

from app.schemas.order import OrderResponse
from app.schemas.quote import QuoteLine, QuoteResponse


class OrderRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def insert(
        self,
        quote: QuoteResponse,
        customer_id: UUID,
        confirmed_at: datetime,
        placed_at: datetime,
    ) -> UUID:
        return await self.connection.fetchval(
            """
            INSERT INTO public.orders (
                cart_id, quote_id, customer_id, cart_version, currency,
                subtotal_cents, tax_rate_bps, tax_cents, total_cents,
                confirmed_at, placed_at, pickup_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            RETURNING id
            """,
            quote.cart_id,
            quote.quote_id,
            customer_id,
            quote.cart_version,
            quote.currency,
            quote.subtotal_cents,
            quote.tax_rate_bps,
            quote.tax_cents,
            quote.total_cents,
            confirmed_at,
            placed_at,
            quote.pickup_at,
        )

    async def insert_items(self, order_id: UUID, items: list[QuoteLine]) -> None:
        for position, line in enumerate(items):
            order_item_id = await self.connection.fetchval(
                """
                INSERT INTO public.order_items (
                    order_id, position, source_line_id, menu_item_id,
                    item_name, quantity, unit_price_cents, line_total_cents
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                RETURNING id
                """,
                order_id,
                position,
                line.line_id,
                line.item_id,
                line.item_name,
                line.quantity,
                line.unit_price_cents,
                line.line_total_cents,
            )

            if line.options:
                await self.connection.executemany(
                    """
                    INSERT INTO public.order_item_modifiers (
                        order_item_id, modifier_option_id, position,
                        name, price_delta_cents
                    ) VALUES ($1,$2,$3,$4,$5)
                    """,
                    [
                        (
                            order_item_id,
                            option.option_id,
                            index,
                            option.name,
                            option.price_delta_cents,
                        )
                        for index, option in enumerate(line.options)
                    ],
                )

    async def append_placed_event(self, order_id: UUID, placed_at: datetime) -> None:
        await self.connection.execute(
            """
            INSERT INTO public.order_events (
                order_id, event_type, data, occurred_at
            )
            VALUES (
                $1,
                'ORDER_PLACED',
                jsonb_build_object('payment_status', 'UNPAID'),
                $2
            )
            """,
            order_id,
            placed_at,
        )

    async def get(self, order_id: UUID, customer_id: UUID) -> OrderResponse | None:
        row = await self.connection.fetchrow(
            """
            SELECT id AS order_id, order_number, cart_id, quote_id, cart_version,
                   status, payment_status, currency, subtotal_cents, tax_rate_bps,
                   tax_cents, total_cents, confirmed_at, placed_at, pickup_at
            FROM public.orders
            WHERE id = $1 AND customer_id = $2
            """,
            order_id,
            customer_id,
        )
        if row is None:
            return None

        lines = await self.connection.fetch(
            """
            SELECT i.source_line_id AS line_id, i.menu_item_id AS item_id,
                   i.item_name, i.quantity, i.unit_price_cents, i.line_total_cents,
                   COALESCE(
                       jsonb_agg(jsonb_build_object(
                           'option_id', m.modifier_option_id,
                           'name', m.name,
                           'price_delta_cents', m.price_delta_cents
                       ) ORDER BY m.position)
                       FILTER (WHERE m.modifier_option_id IS NOT NULL),
                       '[]'::jsonb
                   )::text AS options
            FROM public.order_items AS i
            LEFT JOIN public.order_item_modifiers AS m
                ON m.order_item_id = i.id
            WHERE i.order_id = $1
            GROUP BY i.id
            ORDER BY i.position
            """,
            order_id,
        )

        items = []
        for line in lines:
            data = dict(line)
            data["options"] = json.loads(data["options"])
            items.append(data)

        return OrderResponse.model_validate({**dict(row), "items": items})

    async def get_for_cart(
        self, cart_id: UUID, customer_id: UUID
    ) -> OrderResponse | None:
        order_id = await self.connection.fetchval(
            """
            SELECT id FROM public.orders
            WHERE cart_id = $1 AND customer_id = $2
            """,
            cart_id,
            customer_id,
        )
        if order_id is None:
            return None

        return await self.get(order_id, customer_id)

    async def lock_catalog(self) -> None:
        # Short checkout transaction only.
        await self.connection.execute(
            """
            LOCK TABLE public.restaurant_settings, public.opening_hours,
                       public.menu_categories, public.menu_items,
                       public.modifier_groups, public.modifier_options
            IN SHARE MODE
            """
        )
