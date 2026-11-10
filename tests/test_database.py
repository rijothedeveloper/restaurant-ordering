import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.repositories.cart import CartRepository
from app.repositories.menu import MenuRepository
from app.repositories.menu_admin import MenuAdminRepository
from app.repositories.order import OrderRepository
from app.repositories.quote import QuoteRepository
from app.repositories.restaurant_config import RestaurantConfigurationRepository
from app.schemas.cart import AddCartItemRequest, UpdateCartItemRequest
from app.schemas.order import CheckoutNeedsReview, OrderResponse
from app.schemas.restaurant_config import OpeningPeriod, RestaurantConfiguration
from app.schemas.selection import SelectionRequest
from app.services.cart import (
    CartError,
    add_cart_item,
    get_cart,
    remove_cart_item,
    update_cart_item,
)
from app.services.menu_selection import SelectionError, validate_selection
from app.services.order import OrderError, get_order, place_order
from app.services.quote import (
    QuoteError,
    build_quote,
    complete_quote_readback,
    confirm_quote,
    create_quote,
    get_quote,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def database_settings() -> Settings:
    test_url = os.getenv("TEST_DATABASE_URL")

    if not test_url:
        pytest.fail("Set TEST_DATABASE_URL; use uv run --env-file .env.dbmate")

    return Settings(
        _env_file=None,
        environment="test",
        debug=False,
        database_url=SecretStr(test_url),
        db_pool_min_size=1,
        db_pool_max_size=2,
    )


@asynccontextmanager
async def database_app(settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with app.state.db_pool.acquire(timeout=5) as connection:
            actual_database = await connection.fetchval("SELECT current_database()")

            if actual_database != "restaurant_test":
                raise RuntimeError("Integration tests require restaurant_test")

        yield app


@asynccontextmanager
async def isolated_data(settings: Settings) -> AsyncIterator[asyncpg.Connection]:
    async with database_app(settings) as app:
        async with app.state.db_pool.acquire(timeout=5) as connection:
            transaction = connection.transaction()
            await transaction.start()

            try:
                await connection.execute("DELETE FROM public.orders")
                await connection.execute("DELETE FROM public.cart_item_modifiers")
                await connection.execute("DELETE FROM public.cart_items")
                await connection.execute("DELETE FROM public.carts")
                await connection.execute("DELETE FROM public.customers")
                await connection.execute("DELETE FROM public.modifier_options")
                await connection.execute("DELETE FROM public.modifier_groups")
                await connection.execute("DELETE FROM public.menu_items")
                await connection.execute("DELETE FROM public.menu_categories")
                await connection.execute("DELETE FROM public.opening_hours")
                await connection.execute("DELETE FROM public.restaurant_settings")

                yield connection

            finally:
                await transaction.rollback()


def test_ready_with_postgres(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with database_app(database_settings) as app:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get("/ready")

            assert response.status_code == 200
            assert response.json() == {"status": "ready"}

    asyncio.run(scenario())


def test_singleton_settings_and_opening_hours(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            repository = RestaurantConfigurationRepository(connection)

            assert await repository.get() is None

            await connection.execute(
                "INSERT INTO public.restaurant_settings (name) VALUES ($1)",
                "Test Kitchen",
            )

            config = await repository.get()

            assert config is not None
            assert config.restaurant_name == "Test Kitchen"
            assert config.hours == []
            assert config.preparation_minutes == 20
            assert config.ordering_enabled is False

            with pytest.raises(asyncpg.UniqueViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO public.restaurant_settings (name) VALUES ('Second')"
                    )

            with pytest.raises(asyncpg.CheckViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO public.restaurant_settings (id, name) VALUES (2, 'Second')"
                    )

            for name in ("", "   "):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            "UPDATE public.restaurant_settings SET name = $1 WHERE id = 1",
                            name,
                        )

            for minutes in (0, 181):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            "UPDATE public.restaurant_settings SET preparation_minutes = $1 WHERE id = 1",
                            minutes,
                        )

            await connection.execute(
                "INSERT INTO public.opening_hours VALUES (0, 660, 900), (0, 1020, 1260)"
            )

            with pytest.raises(asyncpg.ExclusionViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO public.opening_hours VALUES (0, 850, 1000)"
                    )

            await connection.execute(
                "INSERT INTO public.opening_hours VALUES (0, 900, 1020)"
            )

            with pytest.raises(asyncpg.CheckViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO public.opening_hours VALUES (1, 100, 100)"
                    )

            loaded = await repository.get()
            assert loaded is not None and len(loaded.hours) == 3

    asyncio.run(scenario())


def test_committed_settings_survive_new_app(database_settings: Settings) -> None:
    async def scenario() -> None:
        created = False

        try:
            async with database_app(database_settings) as first_app:
                async with first_app.state.db_pool.acquire(timeout=5) as connection:
                    existing = await connection.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM public.restaurant_settings) "
                        "OR EXISTS (SELECT 1 FROM public.opening_hours)"
                    )

                    if existing:
                        pytest.fail(
                            "Persistence test requires empty restaurant_test settings "
                            "and hours; existing data was left unchanged"
                        )

                    async with connection.transaction():
                        await connection.execute(
                            """
                            INSERT INTO public.restaurant_settings
                                (name, preparation_minutes, ordering_enabled)
                            VALUES ('Persistence Test', 35, true)
                            """
                        )

                        await connection.execute(
                            "INSERT INTO public.opening_hours VALUES (0, 660, 1260)"
                        )

                    created = True

                    expected = await RestaurantConfigurationRepository(connection).get()

                    assert expected is not None
                    assert expected.restaurant_name == "Persistence Test"
                    assert expected.preparation_minutes == 35
                    assert expected.ordering_enabled is True
                    assert len(expected.hours) == 1

                    timestamp = await connection.fetchval(
                        "SELECT created_at FROM public.restaurant_settings WHERE id = 1"
                    )

                    assert timestamp.tzinfo is not None

            async with database_app(database_settings) as second_app:
                async with second_app.state.db_pool.acquire(timeout=5) as connection:
                    loaded = await RestaurantConfigurationRepository(connection).get()
                    assert loaded == expected

        finally:
            if created:
                async with database_app(database_settings) as cleanup_app:
                    async with cleanup_app.state.db_pool.acquire(
                        timeout=5
                    ) as connection:
                        async with connection.transaction():
                            await connection.execute(
                                "DELETE FROM public.opening_hours "
                                "WHERE weekday = 0 AND opens_minute = 660"
                            )

                            await connection.execute(
                                "DELETE FROM public.restaurant_settings WHERE id = 1"
                            )

    asyncio.run(scenario())


def test_menu_and_constraints(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            repository = MenuRepository(connection)

            assert await repository.get() is None

            await connection.execute(
                "INSERT INTO public.restaurant_settings (name) VALUES ('Menu Test')"
            )

            empty = await repository.get()
            assert empty is not None and empty.categories == []

            categories = {}

            for name, order in [("Drinks", 20), ("Mains", 10), ("Empty", 30)]:
                categories[name] = await connection.fetchval(
                    "INSERT INTO public.menu_categories (name, sort_order) VALUES ($1, $2) RETURNING id",
                    name,
                    order,
                )

            sql = """
                INSERT INTO public.menu_items
                    (category_id, name, price_cents, is_available, sort_order)
                VALUES ($1, $2, $3, $4, $5)
            """

            for category, name, price, available, order in [
                ("Mains", "Second Bowl", 1399, True, 20),
                ("Mains", "First Bowl", 1199, False, 10),
                ("Drinks", "Water", 0, True, 10),
            ]:
                await connection.execute(
                    sql,
                    categories[category],
                    name,
                    price,
                    available,
                    order,
                )

            menu = await repository.get()

            assert menu is not None
            assert [c.name for c in menu.categories] == ["Mains", "Drinks", "Empty"]
            assert [i.name for i in menu.categories[0].items] == [
                "First Bowl",
                "Second Bowl",
            ]
            assert menu.categories[0].items[0].is_available is False
            assert menu.categories[1].items[0].price_cents == 0
            assert menu.categories[2].items == []

            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with connection.transaction():
                    await connection.execute(
                        sql,
                        uuid4(),
                        "Bad category",
                        100,
                        True,
                        0,
                    )

            for name, price in [("Negative price", -1), ("   ", 100)]:
                with pytest.raises(asyncpg.CheckViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            sql,
                            categories["Mains"],
                            name,
                            price,
                            True,
                            0,
                        )

    asyncio.run(scenario())


def test_committed_menu_survives_new_app(database_settings: Settings) -> None:
    async def scenario() -> None:
        category_id, item_id = uuid4(), uuid4()
        group_id, option_id = uuid4(), uuid4()
        created_settings = False

        try:
            async with database_app(database_settings) as first_app:
                async with first_app.state.db_pool.acquire(timeout=5) as connection:
                    async with connection.transaction():
                        created = await connection.fetchval(
                            """
                            INSERT INTO public.restaurant_settings (name)
                            VALUES ('Persistence Test')
                            ON CONFLICT (id) DO NOTHING RETURNING id
                            """
                        )

                        await connection.execute(
                            "INSERT INTO public.menu_categories (id, name) VALUES ($1, 'Persistence')",
                            category_id,
                        )

                        await connection.execute(
                            """
                            INSERT INTO public.menu_items
                                (id, category_id, name, price_cents)
                            VALUES ($1, $2, 'Saved Bowl', 1399)
                            """,
                            item_id,
                            category_id,
                        )

                        await connection.execute(
                            """
                            INSERT INTO public.modifier_groups
                                (id, menu_item_id, name, min_selections, max_selections)
                            VALUES ($1, $2, 'Size', 1, 1)
                            """,
                            group_id,
                            item_id,
                        )

                        await connection.execute(
                            """
                            INSERT INTO public.modifier_options
                                (id, group_id, name, price_delta_cents)
                            VALUES ($1, $2, 'Large', 300)
                            """,
                            option_id,
                            group_id,
                        )

                    created_settings = created is not None

                    admin = MenuAdminRepository(connection)
                    assert await admin.update_item(item_id, 1499, None)
                    assert await admin.update_option(option_id, 350, None)

            async with database_app(database_settings) as second_app:
                async with AsyncClient(
                    transport=ASGITransport(app=second_app),
                    base_url="http://test",
                ) as client:
                    response = await client.get("/menu")

                assert response.status_code == 200

                body = response.json()

                assert body["currency"] == "USD"
                assert "restaurant_id" not in body and "location_id" not in body

                saved = [
                    item
                    for category in body["categories"]
                    for item in category["items"]
                    if item["id"] == str(item_id)
                ]

                assert len(saved) == 1
                assert saved[0]["price_cents"] == 1499
                assert len(saved[0]["modifier_groups"]) == 1

                group = saved[0]["modifier_groups"][0]

                assert group["id"] == str(group_id)
                assert (group["min_selections"], group["max_selections"]) == (1, 1)
                assert group["options"] == [
                    {
                        "id": str(option_id),
                        "name": "Large",
                        "price_delta_cents": 350,
                        "is_available": True,
                    }
                ]

        finally:
            async with database_app(database_settings) as cleanup_app:
                async with cleanup_app.state.db_pool.acquire(timeout=5) as connection:
                    async with connection.transaction():
                        await connection.execute(
                            "DELETE FROM public.modifier_options WHERE id = $1",
                            option_id,
                        )

                        await connection.execute(
                            "DELETE FROM public.modifier_groups WHERE id = $1",
                            group_id,
                        )

                        await connection.execute(
                            "DELETE FROM public.menu_items WHERE id = $1",
                            item_id,
                        )

                        await connection.execute(
                            "DELETE FROM public.menu_categories WHERE id = $1",
                            category_id,
                        )

                        if created_settings:
                            await connection.execute(
                                "DELETE FROM public.restaurant_settings WHERE id = 1"
                            )

    asyncio.run(scenario())


def test_modifiers_and_constraints(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            await connection.execute(
                "INSERT INTO public.restaurant_settings (name) VALUES ('Modifier Test')"
            )

            category_id = await connection.fetchval(
                "INSERT INTO public.menu_categories (name) VALUES ('Mains') RETURNING id"
            )

            item_id = await connection.fetchval(
                """
                INSERT INTO public.menu_items
                    (category_id, name, price_cents, sort_order)
                VALUES ($1, 'Bowl', 1399, 10) RETURNING id
                """,
                category_id,
            )

            await connection.execute(
                """
                INSERT INTO public.menu_items
                    (category_id, name, price_cents, sort_order)
                VALUES ($1, 'Plain Bowl', 999, 20)
                """,
                category_id,
            )

            groups = {}

            group_sql = """
                INSERT INTO public.modifier_groups
                    (menu_item_id, name, min_selections, max_selections, sort_order)
                VALUES ($1, $2, $3, $4, $5) RETURNING id
            """

            for name, minimum, maximum, order in [
                ("Extras", 0, 2, 20),
                ("Size", 1, 1, 10),
                ("Empty", 0, 1, 30),
            ]:
                groups[name] = await connection.fetchval(
                    group_sql,
                    item_id,
                    name,
                    minimum,
                    maximum,
                    order,
                )

            option_sql = """
                INSERT INTO public.modifier_options
                    (group_id, name, price_delta_cents, is_available, sort_order)
                VALUES ($1, $2, $3, $4, $5)
            """

            for group, name, price, available, order in [
                ("Size", "Large", 300, True, 20),
                ("Size", "Regular", 0, True, 10),
                ("Extras", "Cheese", 150, False, 20),
                ("Extras", "Chicken", 400, True, 10),
            ]:
                await connection.execute(
                    option_sql,
                    groups[group],
                    name,
                    price,
                    available,
                    order,
                )

            menu = await MenuRepository(connection).get()
            assert menu is not None

            items = menu.categories[0].items
            assert [item.name for item in items] == ["Bowl", "Plain Bowl"]

            loaded = items[0].modifier_groups

            assert [g.name for g in loaded] == ["Size", "Extras", "Empty"]
            assert [(g.min_selections, g.max_selections) for g in loaded] == [
                (1, 1),
                (0, 2),
                (0, 1),
            ]
            assert [o.name for o in loaded[0].options] == ["Regular", "Large"]
            assert [o.price_delta_cents for o in loaded[0].options] == [0, 300]
            assert [o.name for o in loaded[1].options] == ["Chicken", "Cheese"]
            assert loaded[1].options[1].is_available is False
            assert loaded[2].options == []
            assert items[1].modifier_groups == []

            for name, minimum, maximum, order in [
                ("Bad", -1, 1, 0),
                ("Bad", 0, 0, 0),
                ("Bad", 2, 1, 0),
                ("   ", 0, 1, 0),
                ("Bad", 0, 1, -1),
            ]:
                with pytest.raises(asyncpg.CheckViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            group_sql,
                            item_id,
                            name,
                            minimum,
                            maximum,
                            order,
                        )

            for name, price, order in [
                ("Bad", -1, 0),
                ("   ", 0, 0),
                ("Bad", 0, -1),
            ]:
                with pytest.raises(asyncpg.CheckViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            option_sql,
                            groups["Size"],
                            name,
                            price,
                            True,
                            order,
                        )

            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with connection.transaction():
                    await connection.execute(
                        group_sql,
                        uuid4(),
                        "Bad",
                        0,
                        1,
                        0,
                    )

            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with connection.transaction():
                    await connection.execute(
                        option_sql,
                        uuid4(),
                        "Bad",
                        0,
                        True,
                        0,
                    )

    asyncio.run(scenario())


def test_menu_admin_and_selection(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            await connection.execute(
                "INSERT INTO public.restaurant_settings (name) VALUES ('Admin Test')"
            )

            category_id = await connection.fetchval(
                "INSERT INTO public.menu_categories (name) VALUES ('Mains') RETURNING id"
            )

            item_id = await connection.fetchval(
                "INSERT INTO public.menu_items (category_id,name,price_cents) VALUES ($1,'Bowl',1399) RETURNING id",
                category_id,
            )

            group_id = await connection.fetchval(
                "INSERT INTO public.modifier_groups (menu_item_id,name,min_selections,max_selections) VALUES ($1,'Size',1,1) RETURNING id",
                item_id,
            )

            option_id = await connection.fetchval(
                "INSERT INTO public.modifier_options (group_id,name,price_delta_cents) VALUES ($1,'Large',300) RETURNING id",
                group_id,
            )

            admin = MenuAdminRepository(connection)
            repository = MenuRepository(connection)

            selection = SelectionRequest(
                item_id=item_id,
                option_ids=[option_id],
            )

            menu = await repository.get()

            assert menu is not None
            assert validate_selection(menu, selection).unit_price_cents == 1699

            assert await admin.update_item(item_id, None, False)

            menu = await repository.get()
            assert menu is not None

            with pytest.raises(SelectionError) as error:
                validate_selection(menu, selection)

            assert error.value.code == "item_unavailable"

            assert await admin.update_item(item_id, 1499, True)
            assert await admin.update_option(option_id, 350, False)

            menu = await repository.get()
            assert menu is not None

            with pytest.raises(SelectionError) as error:
                validate_selection(menu, selection)

            assert error.value.code == "option_unavailable"

            assert await admin.update_option(option_id, None, True)

            menu = await repository.get()
            assert menu is not None

            result = validate_selection(menu, selection)

            assert result.unit_price_cents == 1849
            assert result.options[0].price_delta_cents == 350

            assert await admin.update_option(option_id, 0, None)

            menu = await repository.get()

            assert menu is not None
            assert validate_selection(menu, selection).unit_price_cents == 1499
            assert await admin.update_item(uuid4(), 100, True) is False
            assert await admin.update_option(uuid4(), 100, True) is False

    asyncio.run(scenario())


def test_customers_and_cart_identity(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            first_id = await connection.fetchval(
                "INSERT INTO public.customers DEFAULT VALUES RETURNING id"
            )

            second_id = await connection.fetchval(
                "INSERT INTO public.customers DEFAULT VALUES RETURNING id"
            )

            assert first_id != second_id

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.customers WHERE phone_e164 IS NULL"
                )
                == 2
            )

            await connection.execute(
                "INSERT INTO public.customers (phone_e164) VALUES ($1)",
                "+12025550123",
            )

            with pytest.raises(asyncpg.UniqueViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO public.customers (phone_e164) VALUES ($1)",
                        "+12025550123",
                    )

            for invalid_phone in (
                "",
                "anonymous",
                "12025550123",
                "+0123456789",
                "+1 202 555 0123",
                "+1234567890123456",
            ):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            "INSERT INTO public.customers (phone_e164) VALUES ($1)",
                            invalid_phone,
                        )

            first_cart = await connection.fetchrow(
                "INSERT INTO public.carts (customer_id) VALUES ($1) RETURNING *",
                first_id,
            )

            second_cart = await connection.fetchrow(
                "INSERT INTO public.carts (customer_id) VALUES ($1) RETURNING *",
                second_id,
            )

            assert first_cart["id"] != second_cart["id"]
            assert first_cart["customer_id"] == first_id
            assert second_cart["customer_id"] == second_id
            assert first_cart["status"] == "BUILDING"
            assert first_cart["created_at"].tzinfo is not None

            # One customer can start another independent cart.
            another_cart = await connection.fetchval(
                "INSERT INTO public.carts (customer_id) VALUES ($1) RETURNING id",
                first_id,
            )

            assert another_cart != first_cart["id"]

            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO public.carts (customer_id) VALUES ($1)",
                        uuid4(),
                    )

            with pytest.raises(asyncpg.CheckViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE public.carts SET status = 'PAID' WHERE id = $1",
                        first_cart["id"],
                    )

    asyncio.run(scenario())


def test_cart_lines_modifiers_and_deletion(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            customer_id = await connection.fetchval(
                "INSERT INTO public.customers DEFAULT VALUES RETURNING id"
            )

            cart_id = await connection.fetchval(
                "INSERT INTO public.carts (customer_id) VALUES ($1) RETURNING id",
                customer_id,
            )

            category_id = await connection.fetchval(
                "INSERT INTO public.menu_categories (name) VALUES ('Mains') RETURNING id"
            )

            item_id = await connection.fetchval(
                """
                INSERT INTO public.menu_items (category_id, name, price_cents)
                VALUES ($1, 'Bowl', 1399) RETURNING id
                """,
                category_id,
            )

            group_id = await connection.fetchval(
                """
                INSERT INTO public.modifier_groups
                    (menu_item_id, name, min_selections, max_selections)
                VALUES ($1, 'Size', 1, 1) RETURNING id
                """,
                item_id,
            )

            regular_id = await connection.fetchval(
                """
                INSERT INTO public.modifier_options
                    (group_id, name, price_delta_cents)
                VALUES ($1, 'Regular', 0) RETURNING id
                """,
                group_id,
            )

            large_id = await connection.fetchval(
                """
                INSERT INTO public.modifier_options
                    (group_id, name, price_delta_cents)
                VALUES ($1, 'Large', 300) RETURNING id
                """,
                group_id,
            )

            insert_line = """
                INSERT INTO public.cart_items (cart_id, menu_item_id, quantity)
                VALUES ($1, $2, $3) RETURNING id
            """

            regular_line = await connection.fetchval(
                insert_line,
                cart_id,
                item_id,
                1,
            )

            large_line = await connection.fetchval(
                insert_line,
                cart_id,
                item_id,
                2,
            )

            assert regular_line != large_line

            insert_modifier = """
                INSERT INTO public.cart_item_modifiers
                    (cart_item_id, modifier_option_id) VALUES ($1, $2)
            """

            await connection.execute(
                insert_modifier,
                regular_line,
                regular_id,
            )

            await connection.execute(
                insert_modifier,
                large_line,
                large_id,
            )

            rows = await connection.fetch(
                """
                SELECT ci.id, ci.quantity, cim.modifier_option_id
                FROM public.cart_items AS ci
                JOIN public.cart_item_modifiers AS cim ON cim.cart_item_id = ci.id
                WHERE ci.cart_id = $1
                """,
                cart_id,
            )

            assert {
                (r["id"], r["quantity"], r["modifier_option_id"]) for r in rows
            } == {
                (regular_line, 1, regular_id),
                (large_line, 2, large_id),
            }

            for quantity in (-1, 0, 21):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with connection.transaction():
                        await connection.fetchval(
                            insert_line,
                            cart_id,
                            item_id,
                            quantity,
                        )

            for bad_cart, bad_item in (
                (uuid4(), item_id),
                (cart_id, uuid4()),
            ):
                with pytest.raises(asyncpg.ForeignKeyViolationError):
                    async with connection.transaction():
                        await connection.fetchval(
                            insert_line,
                            bad_cart,
                            bad_item,
                            1,
                        )

            with pytest.raises(asyncpg.UniqueViolationError):
                async with connection.transaction():
                    await connection.execute(
                        insert_modifier,
                        regular_line,
                        regular_id,
                    )

            for bad_line, bad_option in (
                (uuid4(), regular_id),
                (regular_line, uuid4()),
            ):
                with pytest.raises(asyncpg.ForeignKeyViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            insert_modifier,
                            bad_line,
                            bad_option,
                        )

            # Referenced catalog entries and customers cannot be removed.
            for statement, record_id in (
                ("DELETE FROM public.customers WHERE id = $1", customer_id),
                ("DELETE FROM public.menu_items WHERE id = $1", item_id),
                ("DELETE FROM public.modifier_options WHERE id = $1", regular_id),
            ):
                with pytest.raises(asyncpg.ForeignKeyViolationError):
                    async with connection.transaction():
                        await connection.execute(statement, record_id)

            await connection.execute(
                "DELETE FROM public.carts WHERE id = $1",
                cart_id,
            )

            assert (
                await connection.fetchval("SELECT count(*) FROM public.cart_items") == 0
            )

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.cart_item_modifiers"
                )
                == 0
            )

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.customers WHERE id = $1",
                    customer_id,
                )
                == 1
            )

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.modifier_options WHERE group_id = $1",
                    group_id,
                )
                == 2
            )

    asyncio.run(scenario())


async def create_cart_test_data(
    connection: asyncpg.Connection,
) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    await connection.execute(
        "INSERT INTO public.restaurant_settings (name) VALUES ('Cart Test')"
    )

    customer_id = await connection.fetchval(
        "INSERT INTO public.customers DEFAULT VALUES RETURNING id"
    )

    cart_id = await connection.fetchval(
        "INSERT INTO public.carts (customer_id) VALUES ($1) RETURNING id",
        customer_id,
    )

    category_id = await connection.fetchval(
        "INSERT INTO public.menu_categories (name) VALUES ('Mains') RETURNING id"
    )

    item_id = await connection.fetchval(
        """
        INSERT INTO public.menu_items (category_id, name, price_cents)
        VALUES ($1, 'Bowl', 1399) RETURNING id
        """,
        category_id,
    )

    group_id = await connection.fetchval(
        """
        INSERT INTO public.modifier_groups
            (menu_item_id, name, min_selections, max_selections)
        VALUES ($1, 'Size', 1, 1) RETURNING id
        """,
        item_id,
    )

    regular_id = await connection.fetchval(
        """
        INSERT INTO public.modifier_options (group_id, name, price_delta_cents)
        VALUES ($1, 'Regular', 0) RETURNING id
        """,
        group_id,
    )

    large_id = await connection.fetchval(
        """
        INSERT INTO public.modifier_options (group_id, name, price_delta_cents)
        VALUES ($1, 'Large', 300) RETURNING id
        """,
        group_id,
    )

    return cart_id, customer_id, item_id, regular_id, large_id


def test_add_cart_item_persists_validated_lines(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            (
                cart_id,
                customer_id,
                item_id,
                regular_id,
                large_id,
            ) = await create_cart_test_data(connection)

            regular_request = AddCartItemRequest(
                item_id=item_id,
                option_ids=[regular_id],
            )

            large_request = AddCartItemRequest(
                item_id=item_id,
                option_ids=[large_id],
                quantity=2,
            )

            regular = await add_cart_item(
                connection,
                cart_id=cart_id,
                customer_id=customer_id,
                expected_version=0,
                item=regular_request,
            )

            large = await add_cart_item(
                connection,
                cart_id=cart_id,
                customer_id=customer_id,
                expected_version=1,
                item=large_request,
            )

            repeated = await add_cart_item(
                connection,
                cart_id=cart_id,
                customer_id=customer_id,
                expected_version=2,
                item=large_request,
            )

            assert len({regular.line_id, large.line_id, repeated.line_id}) == 3
            assert regular.line_total_cents == 1399
            assert large.unit_price_cents == 1699
            assert large.line_total_cents == 3398

            category_id = await connection.fetchval(
                "SELECT category_id FROM public.menu_items WHERE id=$1",
                item_id,
            )

            plain_id = await connection.fetchval(
                """
                INSERT INTO public.menu_items (category_id, name, price_cents)
                VALUES ($1, 'Plain bowl', 999) RETURNING id
                """,
                category_id,
            )

            plain = await add_cart_item(
                connection,
                cart_id=cart_id,
                customer_id=customer_id,
                expected_version=3,
                item=AddCartItemRequest(item_id=plain_id),
            )

            assert plain.options == []
            assert plain.line_total_cents == 999

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.cart_item_modifiers WHERE cart_item_id=$1",
                    plain.line_id,
                )
                == 0
            )

            for result, option_id in (
                (regular, regular_id),
                (large, large_id),
            ):
                row = await connection.fetchrow(
                    "SELECT cart_id, menu_item_id, quantity FROM public.cart_items WHERE id=$1",
                    result.line_id,
                )

                assert row["cart_id"] == cart_id
                assert row["menu_item_id"] == item_id
                assert row["quantity"] == result.quantity

                options = await connection.fetch(
                    "SELECT modifier_option_id FROM public.cart_item_modifiers WHERE cart_item_id=$1",
                    result.line_id,
                )

                assert [r["modifier_option_id"] for r in options] == [option_id]

            for chosen, expected in (
                ([], "too_few_options"),
                ([large_id, large_id], "duplicate_option"),
                ([uuid4()], "invalid_option"),
            ):
                with pytest.raises(SelectionError) as error:
                    await add_cart_item(
                        connection,
                        cart_id=cart_id,
                        customer_id=customer_id,
                        expected_version=4,
                        item=AddCartItemRequest(
                            item_id=item_id,
                            option_ids=chosen,
                        ),
                    )

                assert error.value.code == expected

            for wrong_cart, wrong_customer in (
                (uuid4(), customer_id),
                (cart_id, uuid4()),
            ):
                with pytest.raises(CartError) as error:
                    await add_cart_item(
                        connection,
                        cart_id=wrong_cart,
                        customer_id=wrong_customer,
                        expected_version=4,
                        item=large_request,
                    )

                assert error.value.code == "cart_not_found"

            await connection.execute(
                "UPDATE public.carts SET status='CHECKED_OUT' WHERE id=$1",
                cart_id,
            )

            with pytest.raises(CartError) as error:
                await add_cart_item(
                    connection,
                    cart_id=cart_id,
                    customer_id=customer_id,
                    expected_version=4,
                    item=large_request,
                )

            assert error.value.code == "cart_not_editable"

            await connection.execute(
                "UPDATE public.carts SET status='BUILDING' WHERE id=$1",
                cart_id,
            )

            await connection.execute(
                "UPDATE public.modifier_options SET is_available=false WHERE id=$1",
                large_id,
            )

            with pytest.raises(SelectionError) as error:
                await add_cart_item(
                    connection,
                    cart_id=cart_id,
                    customer_id=customer_id,
                    expected_version=4,
                    item=large_request,
                )

            assert error.value.code == "option_unavailable"

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.cart_items WHERE cart_id=$1",
                    cart_id,
                )
                == 4
            )

            await connection.execute(
                "UPDATE public.menu_items SET price_cents=1499 WHERE id=$1",
                item_id,
            )

            await connection.execute(
                "UPDATE public.modifier_options SET price_delta_cents=350, is_available=true WHERE id=$1",
                large_id,
            )

            repriced = await add_cart_item(
                connection,
                cart_id=cart_id,
                customer_id=customer_id,
                expected_version=4,
                item=large_request,
            )

            assert repriced.unit_price_cents == 1849
            assert repriced.line_total_cents == 3698

    asyncio.run(scenario())


def test_add_cart_item_rolls_back_all_writes(
    database_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            cart_id, customer_id, item_id, _, large_id = await create_cart_test_data(
                connection
            )

            request = AddCartItemRequest(
                item_id=item_id,
                option_ids=[large_id],
                quantity=2,
            )

            original = CartRepository.insert_line

            async def fail_after_insert(self, cart_id, item_id, quantity, option_ids):
                line_id = await original(
                    self,
                    cart_id,
                    item_id,
                    quantity,
                    option_ids,
                )

                # Force a constraint failure after the line and option were written.
                await self.connection.execute(
                    """
                    INSERT INTO public.cart_item_modifiers
                        (cart_item_id, modifier_option_id) VALUES ($1, $2)
                    """,
                    line_id,
                    option_ids[0],
                )

                return line_id

            with monkeypatch.context() as patch:
                patch.setattr(
                    CartRepository,
                    "insert_line",
                    fail_after_insert,
                )

                with pytest.raises(asyncpg.UniqueViolationError):
                    await add_cart_item(
                        connection,
                        cart_id=cart_id,
                        customer_id=customer_id,
                        expected_version=0,
                        item=request,
                    )

            assert (
                await connection.fetchval("SELECT count(*) FROM public.cart_items") == 0
            )

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.cart_item_modifiers"
                )
                == 0
            )

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.carts WHERE id=$1",
                    cart_id,
                )
                == 1
            )

            # The connection is usable after rollback, and a later add can succeed.
            result = await add_cart_item(
                connection,
                cart_id=cart_id,
                customer_id=customer_id,
                expected_version=0,
                item=request,
            )

            assert result.line_total_cents == 3398

            assert (
                await connection.fetchval("SELECT count(*) FROM public.cart_items") == 1
            )

    asyncio.run(scenario())


def test_cart_read_update_and_remove(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            (
                cart_id,
                customer_id,
                item_id,
                regular_id,
                large_id,
            ) = await create_cart_test_data(connection)

            context = {
                "cart_id": cart_id,
                "customer_id": customer_id,
            }

            empty = await get_cart(connection, **context)

            assert empty.items == []
            assert empty.subtotal_cents == 0

            regular = await add_cart_item(
                connection,
                **context,
                expected_version=0,
                item=AddCartItemRequest(
                    item_id=item_id,
                    option_ids=[regular_id],
                ),
            )

            large = await add_cart_item(
                connection,
                **context,
                expected_version=1,
                item=AddCartItemRequest(
                    item_id=item_id,
                    option_ids=[large_id],
                    quantity=2,
                ),
            )

            cart = await get_cart(connection, **context)
            assert cart.subtotal_cents == 4797

            # Both timestamps use the outer transaction's now(); UUID breaks the tie.
            assert [line.line_id for line in cart.items] == sorted(
                [regular.line_id, large.line_id],
                key=str,
            )

            assert (
                await get_cart(connection, **context)
            ).model_dump() == cart.model_dump()

            cart = await update_cart_item(
                connection,
                **context,
                expected_version=2,
                line_id=regular.line_id,
                item=UpdateCartItemRequest(
                    quantity=3,
                    option_ids=[regular_id],
                ),
            )

            assert cart.subtotal_cents == 7595

            cart = await update_cart_item(
                connection,
                **context,
                expected_version=3,
                line_id=regular.line_id,
                item=UpdateCartItemRequest(
                    quantity=3,
                    option_ids=[large_id],
                ),
            )

            assert cart.subtotal_cents == 8495
            assert len(cart.items) == 2

            changed = next(
                line for line in cart.items if line.line_id == regular.line_id
            )

            assert changed.quantity == 3
            assert changed.option_ids == [large_id]

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.cart_item_modifiers WHERE cart_item_id=$1",
                    regular.line_id,
                )
                == 1
            )

            with pytest.raises(SelectionError) as error:
                await update_cart_item(
                    connection,
                    **context,
                    expected_version=4,
                    line_id=regular.line_id,
                    item=UpdateCartItemRequest(
                        quantity=1,
                        option_ids=[],
                    ),
                )

            assert error.value.code == "too_few_options"
            assert (await get_cart(connection, **context)).subtotal_cents == 8495

            await connection.execute(
                "UPDATE public.menu_items SET price_cents=1499 WHERE id=$1",
                item_id,
            )

            await connection.execute(
                "UPDATE public.modifier_options SET price_delta_cents=350 WHERE id=$1",
                large_id,
            )

            assert (await get_cart(connection, **context)).subtotal_cents == 9245

            await connection.execute(
                "UPDATE public.modifier_options SET is_available=false WHERE id=$1",
                large_id,
            )

            invalid = await get_cart(connection, **context)

            assert invalid.subtotal_cents is None
            assert len(invalid.items) == 2
            assert all(
                line.issue.code == "option_unavailable" for line in invalid.items
            )
            assert all(line.line_total_cents is None for line in invalid.items)
            assert all(line.item_name == "Bowl" for line in invalid.items)

            remaining = await remove_cart_item(
                connection,
                **context,
                expected_version=4,
                line_id=regular.line_id,
            )

            assert len(remaining.items) == 1
            assert remaining.subtotal_cents is None

            empty = await remove_cart_item(
                connection,
                **context,
                expected_version=5,
                line_id=large.line_id,
            )

            assert empty.items == []
            assert empty.subtotal_cents == 0

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.cart_item_modifiers"
                )
                == 0
            )

            # Explicit [] removes all choices when that group's minimum permits it.
            await connection.execute(
                "UPDATE public.modifier_groups SET min_selections=0 WHERE menu_item_id=$1",
                item_id,
            )

            added = await add_cart_item(
                connection,
                **context,
                expected_version=6,
                item=AddCartItemRequest(
                    item_id=item_id,
                    option_ids=[regular_id],
                ),
            )

            cleared = await update_cart_item(
                connection,
                **context,
                expected_version=7,
                line_id=added.line_id,
                item=UpdateCartItemRequest(
                    quantity=2,
                    option_ids=[],
                ),
            )

            assert cleared.subtotal_cents == 2998
            assert cleared.items[0].option_ids == []
            assert cleared.items[0].options == []
            assert cleared.items[0].issue is None

    asyncio.run(scenario())


def test_cart_operations_enforce_context(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            cart_id, customer_id, item_id, regular_id, _ = await create_cart_test_data(
                connection
            )

            context = {
                "cart_id": cart_id,
                "customer_id": customer_id,
            }

            added = await add_cart_item(
                connection,
                **context,
                expected_version=0,
                item=AddCartItemRequest(
                    item_id=item_id,
                    option_ids=[regular_id],
                ),
            )

            other_customer = await connection.fetchval(
                "INSERT INTO public.customers DEFAULT VALUES RETURNING id"
            )

            other_cart = await connection.fetchval(
                "INSERT INTO public.carts (customer_id) VALUES ($1) RETURNING id",
                other_customer,
            )

            other_line = await add_cart_item(
                connection,
                cart_id=other_cart,
                customer_id=other_customer,
                expected_version=0,
                item=AddCartItemRequest(
                    item_id=item_id,
                    option_ids=[regular_id],
                    quantity=2,
                ),
            )

            edit = UpdateCartItemRequest(
                quantity=3,
                option_ids=[regular_id],
            )

            for operation in ("read", "update", "remove"):
                with pytest.raises(CartError) as error:
                    wrong = {
                        "cart_id": cart_id,
                        "customer_id": other_customer,
                    }

                    if operation == "read":
                        await get_cart(connection, **wrong)

                    elif operation == "update":
                        await update_cart_item(
                            connection,
                            **wrong,
                            expected_version=1,
                            line_id=added.line_id,
                            item=edit,
                        )

                    else:
                        await remove_cart_item(
                            connection,
                            **wrong,
                            expected_version=1,
                            line_id=added.line_id,
                        )

                assert error.value.code == "cart_not_found"

            for line_id in (other_line.line_id, uuid4()):
                for operation in ("update", "remove"):
                    with pytest.raises(CartError) as error:
                        if operation == "update":
                            await update_cart_item(
                                connection,
                                **context,
                                expected_version=1,
                                line_id=line_id,
                                item=edit,
                            )

                        else:
                            await remove_cart_item(
                                connection,
                                **context,
                                expected_version=1,
                                line_id=line_id,
                            )

                    assert error.value.code == "line_not_found"

            assert (await get_cart(connection, **context)).subtotal_cents == 1399

            other = await get_cart(
                connection,
                cart_id=other_cart,
                customer_id=other_customer,
            )

            assert other.subtotal_cents == 2798

            await connection.execute(
                "UPDATE public.carts SET status='CHECKED_OUT' WHERE id=$1",
                cart_id,
            )

            assert (await get_cart(connection, **context)).status == "CHECKED_OUT"

            for operation in ("update", "remove"):
                with pytest.raises(CartError) as error:
                    if operation == "update":
                        await update_cart_item(
                            connection,
                            **context,
                            expected_version=1,
                            line_id=added.line_id,
                            item=edit,
                        )

                    else:
                        await remove_cart_item(
                            connection,
                            **context,
                            expected_version=1,
                            line_id=added.line_id,
                        )

                assert error.value.code == "cart_not_editable"

    asyncio.run(scenario())


def test_cart_edit_rollbacks_preserve_original_line(
    database_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            (
                cart_id,
                customer_id,
                item_id,
                regular_id,
                large_id,
            ) = await create_cart_test_data(connection)

            context = {
                "cart_id": cart_id,
                "customer_id": customer_id,
            }

            added = await add_cart_item(
                connection,
                **context,
                expected_version=0,
                item=AddCartItemRequest(
                    item_id=item_id,
                    option_ids=[regular_id],
                    quantity=2,
                ),
            )

            before = await get_cart(connection, **context)

            original_replace = CartRepository.replace_line
            original_delete = CartRepository.delete_line

            async def fail_after_replace(self, cart_id, line_id, quantity, option_ids):
                updated = await original_replace(
                    self,
                    cart_id,
                    line_id,
                    quantity,
                    option_ids,
                )

                await self.connection.execute(
                    "INSERT INTO public.cart_item_modifiers (cart_item_id,modifier_option_id) VALUES ($1,$2)",
                    line_id,
                    option_ids[0],
                )

                return updated

            with monkeypatch.context() as patch:
                patch.setattr(
                    CartRepository,
                    "replace_line",
                    fail_after_replace,
                )

                with pytest.raises(asyncpg.UniqueViolationError):
                    await update_cart_item(
                        connection,
                        **context,
                        expected_version=1,
                        line_id=added.line_id,
                        item=UpdateCartItemRequest(
                            quantity=4,
                            option_ids=[large_id],
                        ),
                    )

            assert (
                await get_cart(connection, **context)
            ).model_dump() == before.model_dump()

            async def fail_after_delete(self, cart_id, line_id):
                await original_delete(self, cart_id, line_id)
                raise OSError("simulated failure after cascading deletion")

            with monkeypatch.context() as patch:
                patch.setattr(
                    CartRepository,
                    "delete_line",
                    fail_after_delete,
                )

                with pytest.raises(OSError):
                    await remove_cart_item(
                        connection,
                        **context,
                        expected_version=1,
                        line_id=added.line_id,
                    )

            assert (
                await get_cart(connection, **context)
            ).model_dump() == before.model_dump()

            assert (
                await connection.fetchval(
                    "SELECT modifier_option_id FROM public.cart_item_modifiers WHERE cart_item_id=$1",
                    added.line_id,
                )
                == regular_id
            )

            empty = await remove_cart_item(
                connection,
                **context,
                expected_version=1,
                line_id=added.line_id,
            )

            assert empty.items == []
            assert empty.subtotal_cents == 0

    asyncio.run(scenario())


def test_cart_versions_and_conflicts(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            (
                cart_id,
                customer_id,
                item_id,
                regular_id,
                large_id,
            ) = await create_cart_test_data(connection)

            context = {
                "cart_id": cart_id,
                "customer_id": customer_id,
            }

            assert (await get_cart(connection, **context)).version == 0

            with pytest.raises(asyncpg.CheckViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE public.carts SET version=-1 WHERE id=$1",
                        cart_id,
                    )

            added = await add_cart_item(
                connection,
                **context,
                expected_version=0,
                item=AddCartItemRequest(
                    item_id=item_id,
                    option_ids=[regular_id],
                ),
            )

            assert added.version == 1

            before = await get_cart(connection, **context)

            for operation in ("add", "update", "remove"):
                with pytest.raises(CartError) as error:
                    if operation == "add":
                        await add_cart_item(
                            connection,
                            **context,
                            expected_version=0,
                            item=AddCartItemRequest(
                                item_id=item_id,
                                option_ids=[large_id],
                            ),
                        )

                    elif operation == "update":
                        await update_cart_item(
                            connection,
                            **context,
                            expected_version=0,
                            line_id=added.line_id,
                            item=UpdateCartItemRequest(
                                quantity=3,
                                option_ids=[large_id],
                            ),
                        )

                    else:
                        await remove_cart_item(
                            connection,
                            **context,
                            expected_version=0,
                            line_id=added.line_id,
                        )

                assert error.value.code == "cart_version_conflict"
                assert (await get_cart(connection, **context)) == before

            with pytest.raises(SelectionError):
                await update_cart_item(
                    connection,
                    **context,
                    expected_version=1,
                    line_id=added.line_id,
                    item=UpdateCartItemRequest(
                        quantity=2,
                        option_ids=[],
                    ),
                )

            assert (await get_cart(connection, **context)) == before

            changed = await update_cart_item(
                connection,
                **context,
                expected_version=1,
                line_id=added.line_id,
                item=UpdateCartItemRequest(
                    quantity=2,
                    option_ids=[large_id],
                ),
            )

            assert changed.version == 2
            assert changed.subtotal_cents == 3398

            empty = await remove_cart_item(
                connection,
                **context,
                expected_version=changed.version,
                line_id=added.line_id,
            )

            assert empty.version == 3
            assert empty.items == []

            with pytest.raises(CartError) as error:
                await remove_cart_item(
                    connection,
                    **context,
                    expected_version=3,
                    line_id=added.line_id,
                )

            assert error.value.code == "line_not_found"
            assert (await get_cart(connection, **context)).version == 3

    asyncio.run(scenario())


@asynccontextmanager
async def committed_cart_data(
    settings: Settings,
    *,
    checkout_ready: bool = False,
) -> AsyncIterator[SimpleNamespace]:
    d = SimpleNamespace(
        customer_id=uuid4(),
        cart_id=uuid4(),
        category_id=uuid4(),
        item_id=uuid4(),
        group_id=uuid4(),
        regular_id=uuid4(),
        large_id=uuid4(),
    )

    created_settings = False

    try:
        async with database_app(settings) as app:
            async with app.state.db_pool.acquire(timeout=5) as connection:
                async with connection.transaction():
                    created = await connection.fetchval(
                        """
                        INSERT INTO public.restaurant_settings (name)
                        VALUES ('Cart Version Test')
                        ON CONFLICT (id) DO NOTHING RETURNING id
                        """
                    )

                    if checkout_ready:
                        if created is None or await connection.fetchval(
                            "SELECT EXISTS (SELECT 1 FROM public.opening_hours)"
                        ):
                            pytest.fail(
                                "Checkout tests require empty settings and hours"
                            )

                        await connection.execute(
                            """
                            UPDATE public.restaurant_settings
                            SET ordering_enabled=true,
                                tax_rate_bps=1000,
                                preparation_minutes=20
                            WHERE id=1
                            """
                        )
                        await connection.execute(
                            """
                            INSERT INTO public.opening_hours
                            SELECT day, 0, 1440
                            FROM generate_series(0,6) AS day
                            """
                        )

                    await connection.execute(
                        "INSERT INTO public.customers (id) VALUES ($1)",
                        d.customer_id,
                    )

                    await connection.execute(
                        "INSERT INTO public.carts (id,customer_id) VALUES ($1,$2)",
                        d.cart_id,
                        d.customer_id,
                    )

                    await connection.execute(
                        """
                        INSERT INTO public.menu_categories (id,name)
                        VALUES ($1,'Cart Test')
                        """,
                        d.category_id,
                    )

                    await connection.execute(
                        """
                        INSERT INTO public.menu_items
                            (id,category_id,name,price_cents)
                        VALUES ($1,$2,'Bowl',1399)
                        """,
                        d.item_id,
                        d.category_id,
                    )

                    await connection.execute(
                        """
                        INSERT INTO public.modifier_groups
                            (id,menu_item_id,name,min_selections,max_selections)
                        VALUES ($1,$2,'Size',1,1)
                        """,
                        d.group_id,
                        d.item_id,
                    )

                    await connection.executemany(
                        """
                        INSERT INTO public.modifier_options
                            (id,group_id,name,price_delta_cents)
                        VALUES ($1,$2,$3,$4)
                        """,
                        [
                            (d.regular_id, d.group_id, "Regular", 0),
                            (d.large_id, d.group_id, "Large", 300),
                        ],
                    )

                    added = await add_cart_item(
                        connection,
                        cart_id=d.cart_id,
                        customer_id=d.customer_id,
                        expected_version=0,
                        item=AddCartItemRequest(
                            item_id=d.item_id,
                            option_ids=[d.regular_id],
                        ),
                    )

                created_settings = created is not None
                d.line_id = added.line_id

        yield d

    finally:
        async with database_app(settings) as app:
            async with app.state.db_pool.acquire(timeout=5) as connection:
                async with connection.transaction():
                    # 1. Delete orders before carts.

                    await connection.execute(
                        "DELETE FROM public.orders WHERE cart_id=$1",
                        d.cart_id,
                    )

                    # 2. Delete the cart and customer.

                    await connection.execute(
                        "DELETE FROM public.carts WHERE id=$1",
                        d.cart_id,
                    )

                    await connection.execute(
                        "DELETE FROM public.customers WHERE id=$1",
                        d.customer_id,
                    )

                    # 3. Delete ALL modifier options belonging

                    # to groups associated with this test menu item.

                    await connection.execute(
                        """

                        DELETE FROM public.modifier_options

                        WHERE group_id IN (

                            SELECT id

                            FROM public.modifier_groups

                            WHERE menu_item_id=$1

                        )

                        """,
                        d.item_id,
                    )

                    # 4. Delete ALL modifier groups belonging

                    # to this menu item, including groups created

                    # during the concurrency tests.

                    await connection.execute(
                        """

                        DELETE FROM public.modifier_groups

                        WHERE menu_item_id=$1

                        """,
                        d.item_id,
                    )

                    # 5. Delete the menu item.

                    await connection.execute(
                        "DELETE FROM public.menu_items WHERE id=$1",
                        d.item_id,
                    )

                    # 6. Delete its category.

                    await connection.execute(
                        "DELETE FROM public.menu_categories WHERE id=$1",
                        d.category_id,
                    )

                    # 7. Remove settings created by this fixture.

                    if created_settings:
                        if checkout_ready:
                            await connection.execute("DELETE FROM public.opening_hours")

                        await connection.execute(
                            """

                            DELETE FROM public.restaurant_settings

                            WHERE id=1

                            """
                        )


@asynccontextmanager
async def confirmed_checkout_data(settings: Settings):
    async with committed_cart_data(
        settings,
        checkout_ready=True,
    ) as d:
        d.context = {
            "cart_id": d.cart_id,
            "customer_id": d.customer_id,
        }

        async with database_app(settings) as app:
            async with app.state.db_pool.acquire(timeout=5) as connection:
                d.quote = await create_quote(
                    connection,
                    **d.context,
                    expected_version=1,
                )
                await confirm_test_quote(
                    connection,
                    d.context,
                    d.quote,
                )

        d.target = {
            **d.context,
            "quote_id": d.quote.quote_id,
        }
        yield d


def test_concurrent_cart_edits_reject_waiting_stale_writer(
    database_settings: Settings,
) -> None:
    async def scenario() -> None:
        async with committed_cart_data(database_settings) as d:
            context = {
                "cart_id": d.cart_id,
                "customer_id": d.customer_id,
            }

            async with database_app(database_settings) as app:
                async with (
                    app.state.db_pool.acquire(timeout=5) as first,
                    app.state.db_pool.acquire(timeout=5) as second,
                ):
                    pending = None

                    try:
                        async with first.transaction():
                            winner = await update_cart_item(
                                first,
                                **context,
                                line_id=d.line_id,
                                expected_version=1,
                                item=UpdateCartItemRequest(
                                    quantity=2,
                                    option_ids=[d.regular_id],
                                ),
                            )

                            pending = asyncio.create_task(
                                update_cart_item(
                                    second,
                                    **context,
                                    line_id=d.line_id,
                                    expected_version=1,
                                    item=UpdateCartItemRequest(
                                        quantity=3,
                                        option_ids=[d.large_id],
                                    ),
                                )
                            )

                            async with asyncio.timeout(3):
                                while not await first.fetchval(
                                    """
                                    SELECT $1::integer =
                                        ANY(pg_blocking_pids($2::integer))
                                    """,
                                    first.get_server_pid(),
                                    second.get_server_pid(),
                                ):
                                    if pending.done():
                                        pending.result()

                                        pytest.fail(
                                            "The second writer did not wait "
                                            "for the cart lock"
                                        )

                                    await asyncio.sleep(0.01)

                        # The outer transaction committed the winner's version 2.
                        with pytest.raises(CartError) as error:
                            await asyncio.wait_for(pending, timeout=5)

                        assert error.value.code == "cart_version_conflict"

                        current = await get_cart(first, **context)

                        assert current.version == winner.version == 2
                        assert current.items[0].quantity == 2
                        assert current.items[0].option_ids == [d.regular_id]
                        assert current.subtotal_cents == 2798

                    finally:
                        if pending is not None:
                            if not pending.done():
                                pending.cancel()

                            await asyncio.gather(
                                pending,
                                return_exceptions=True,
                            )

    asyncio.run(scenario())


def test_committed_cart_survives_new_app(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with committed_cart_data(database_settings) as d:
            context = {
                "cart_id": d.cart_id,
                "customer_id": d.customer_id,
            }

            async with database_app(database_settings) as first_app:
                async with first_app.state.db_pool.acquire(timeout=5) as connection:
                    saved = await update_cart_item(
                        connection,
                        **context,
                        line_id=d.line_id,
                        expected_version=1,
                        item=UpdateCartItemRequest(
                            quantity=3,
                            option_ids=[d.large_id],
                        ),
                    )

                    assert saved.version == 2
                    assert saved.subtotal_cents == 5097

            assert first_app.state.db_pool is None

            async with database_app(database_settings) as second_app:
                assert second_app is not first_app

                async with second_app.state.db_pool.acquire(timeout=5) as connection:
                    restored = await get_cart(connection, **context)

                    assert restored == saved

                    changed = await update_cart_item(
                        connection,
                        **context,
                        line_id=d.line_id,
                        expected_version=restored.version,
                        item=UpdateCartItemRequest(
                            quantity=4,
                            option_ids=[d.large_id],
                        ),
                    )

                    assert changed.version == 3
                    assert changed.subtotal_cents == 6796

    asyncio.run(scenario())


async def prepare_quote_cart(connection: asyncpg.Connection):
    cart_id, customer_id, item_id, _, large_id = await create_cart_test_data(connection)

    await connection.execute(
        """
        UPDATE public.restaurant_settings
        SET tax_rate_bps=1000, ordering_enabled=true, preparation_minutes=20
        WHERE id=1
        """
    )
    await connection.execute(
        """
        INSERT INTO public.opening_hours (weekday, opens_minute, closes_minute)
        SELECT day, 0, 1440 FROM generate_series(0, 6) AS day
        """
    )

    return {"cart_id": cart_id, "customer_id": customer_id}, item_id, large_id


def test_quotes_preserve_snapshots_and_cart_version(
    database_settings: Settings,
) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            context, item_id, large_id = await prepare_quote_cart(connection)

            added = await add_cart_item(
                connection,
                **context,
                expected_version=0,
                item=AddCartItemRequest(
                    item_id=item_id, option_ids=[large_id], quantity=2
                ),
            )

            first = await create_quote(
                connection, **context, expected_version=added.version
            )

            assert first.cart_version == 1
            assert (first.subtotal_cents, first.tax_cents, first.total_cents) == (
                3398,
                340,
                3738,
            )
            assert first.expires_at - first.created_at == timedelta(minutes=2)
            assert first.pickup_at - first.expires_at == timedelta(minutes=20)
            assert first.items[0].options[0].option_id == large_id
            assert first.items[0].line_id == added.line_id
            assert (await get_cart(connection, **context)).version == 1

            await connection.execute(
                "UPDATE public.menu_items SET price_cents=1499 WHERE id=$1",
                item_id,
            )
            await connection.execute(
                """
                UPDATE public.modifier_options
                SET name='XL', price_delta_cents=350 WHERE id=$1
                """,
                large_id,
            )
            await connection.execute(
                "UPDATE public.restaurant_settings SET tax_rate_bps=500 WHERE id=1"
            )

            # The saved quote still contains the original name and prices.
            assert (
                await get_quote(connection, **context, quote_id=first.quote_id) == first
            )

            newer = await create_quote(connection, **context, expected_version=1)
            assert newer.quote_id != first.quote_id
            assert newer.cart_version == first.cart_version
            assert newer.items[0].options[0].name == "XL"
            assert (newer.subtotal_cents, newer.tax_cents, newer.total_cents) == (
                3698,
                185,
                3883,
            )

            await update_cart_item(
                connection,
                **context,
                expected_version=1,
                line_id=added.line_id,
                item=UpdateCartItemRequest(quantity=3, option_ids=[large_id]),
            )

            with pytest.raises(QuoteError) as stale:
                await get_quote(connection, **context, quote_id=first.quote_id)
            assert stale.value.code == "quote_stale"

            with pytest.raises(CartError) as conflict:
                await create_quote(connection, **context, expected_version=1)
            assert conflict.value.code == "cart_version_conflict"

            count = await connection.fetchval(
                "SELECT count(*) FROM public.quotes WHERE cart_id=$1",
                context["cart_id"],
            )
            assert count == 2

    asyncio.run(scenario())


def test_quote_guards_and_expiry(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            context, item_id, large_id = await prepare_quote_cart(connection)

            with pytest.raises(QuoteError) as empty:
                await create_quote(connection, **context, expected_version=0)
            assert empty.value.code == "empty_cart"

            await add_cart_item(
                connection,
                **context,
                expected_version=0,
                item=AddCartItemRequest(item_id=item_id, option_ids=[large_id]),
            )

            for wrong in (
                {**context, "customer_id": uuid4()},
                {**context, "cart_id": uuid4()},
            ):
                with pytest.raises(CartError) as error:
                    await create_quote(connection, **wrong, expected_version=1)
                assert error.value.code == "cart_not_found"

            await connection.execute(
                "UPDATE public.restaurant_settings SET tax_rate_bps=NULL WHERE id=1"
            )
            with pytest.raises(QuoteError) as error:
                await create_quote(connection, **context, expected_version=1)
            assert error.value.code == "tax_not_configured"

            await connection.execute(
                "UPDATE public.restaurant_settings SET tax_rate_bps=0 WHERE id=1"
            )
            await connection.execute(
                "UPDATE public.modifier_options SET is_available=false WHERE id=$1",
                large_id,
            )
            with pytest.raises(QuoteError) as error:
                await create_quote(connection, **context, expected_version=1)
            assert error.value.code == "invalid_cart"

            count = await connection.fetchval(
                "SELECT count(*) FROM public.quotes WHERE cart_id=$1",
                context["cart_id"],
            )
            assert count == 0

            await connection.execute(
                "UPDATE public.modifier_options SET is_available=true WHERE id=$1",
                large_id,
            )
            quote = await create_quote(connection, **context, expected_version=1)
            assert quote.tax_cents == 0

            for wrong in (
                {**context, "customer_id": uuid4()},
                {**context, "cart_id": uuid4()},
            ):
                with pytest.raises(QuoteError) as error:
                    await get_quote(connection, **wrong, quote_id=quote.quote_id)
                assert error.value.code == "quote_not_found"

            # Insert an old snapshot to exercise expiry without waiting two minutes.
            old = quote.model_copy(
                update={
                    "quote_id": uuid4(),
                    "created_at": quote.created_at - timedelta(hours=1),
                    "expires_at": quote.expires_at - timedelta(hours=1),
                    "pickup_at": quote.pickup_at - timedelta(hours=1),
                }
            )
            await QuoteRepository(connection).insert(old)

            with pytest.raises(QuoteError) as error:
                await get_quote(connection, **context, quote_id=old.quote_id)
            assert error.value.code == "quote_expired"

    asyncio.run(scenario())


def test_failed_quote_insert_rolls_back(
    database_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with isolated_data(database_settings) as connection:
            context, item_id, large_id = await prepare_quote_cart(connection)

            await add_cart_item(
                connection,
                **context,
                expected_version=0,
                item=AddCartItemRequest(item_id=item_id, option_ids=[large_id]),
            )

            original = QuoteRepository.insert

            async def fail_after_insert(self, quote):
                await original(self, quote)
                raise OSError("simulated failure after quote insertion")

            with monkeypatch.context() as patch:
                patch.setattr(QuoteRepository, "insert", fail_after_insert)
                with pytest.raises(OSError):
                    await create_quote(connection, **context, expected_version=1)

            count = await connection.fetchval(
                "SELECT count(*) FROM public.quotes WHERE cart_id=$1",
                context["cart_id"],
            )
            assert count == 0

            cart = await get_cart(connection, **context)
            assert cart.version == 1
            assert cart.subtotal_cents == 1699

    asyncio.run(scenario())


async def prepare_pending_quote(connection: asyncpg.Connection):
    context, item_id, large_id = await prepare_quote_cart(connection)

    await add_cart_item(
        connection,
        **context,
        expected_version=0,
        item=AddCartItemRequest(item_id=item_id, option_ids=[large_id]),
    )

    quote = await create_quote(connection, **context, expected_version=1)
    return context, item_id, large_id, quote


async def quote_state(connection: asyncpg.Connection, quote_id: UUID):
    return await connection.fetchrow(
        """
        SELECT is_pending, readback_token, confirmed_at
        FROM public.quotes WHERE id=$1
        """,
        quote_id,
    )


def test_confirmation_requires_readback_and_explicit_answer(
    database_settings: Settings,
):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, _, _, quote = await prepare_pending_quote(connection)
            target = {**context, "quote_id": quote.quote_id}

            with pytest.raises(QuoteError) as error:
                await confirm_quote(
                    connection,
                    **target,
                    readback_token=uuid4(),
                    answer="yes",
                )
            assert error.value.code == "quote_not_read_back"

            receipt = await complete_quote_readback(connection, **target)

            with pytest.raises(QuoteError) as error:
                await confirm_quote(
                    connection,
                    **target,
                    readback_token=uuid4(),
                    answer="yes",
                )
            assert error.value.code == "quote_readback_mismatch"

            for answer in ("maybe", "yes but add one", "", True):
                with pytest.raises(QuoteError) as error:
                    await confirm_quote(
                        connection,
                        **target,
                        readback_token=receipt.readback_token,
                        answer=answer,
                    )
                assert error.value.code == "explicit_confirmation_required"

            state = await quote_state(connection, quote.quote_id)
            assert state["confirmed_at"] is None

            args = {
                **target,
                "readback_token": receipt.readback_token,
                "answer": " YES ",
            }
            first = await confirm_quote(connection, **args)

            assert first is not None and first.cart_version == 1
            assert first.confirmed_at >= receipt.completed_at
            assert await confirm_quote(connection, **args) == first
            assert (await get_cart(connection, **context)).version == 1

            newer = await complete_quote_readback(connection, **target)
            assert newer.readback_token != receipt.readback_token

            state = await quote_state(connection, quote.quote_id)
            assert state["confirmed_at"] is None

            with pytest.raises(QuoteError) as error:
                await confirm_quote(connection, **args)
            assert error.value.code == "quote_readback_mismatch"

            args["readback_token"] = newer.readback_token
            assert await confirm_quote(connection, **args) is not None

            args["answer"] = "no"
            assert await confirm_quote(connection, **args) is None

            state = await quote_state(connection, quote.quote_id)
            assert state["is_pending"] is False

            args["answer"] = "yes"
            with pytest.raises(QuoteError) as error:
                await confirm_quote(connection, **args)
            assert error.value.code == "quote_not_pending"

    asyncio.run(scenario())


def test_requote_replaces_confirmation_at_same_version(
    database_settings: Settings,
):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, _, _, first = await prepare_pending_quote(connection)
            target = {**context, "quote_id": first.quote_id}

            receipt = await complete_quote_readback(connection, **target)
            await confirm_quote(
                connection,
                **target,
                readback_token=receipt.readback_token,
                answer="yes",
            )

            second = await create_quote(connection, **context, expected_version=1)

            assert first.cart_version == second.cart_version == 1
            assert first.quote_id != second.quote_id
            assert not (await quote_state(connection, first.quote_id))["is_pending"]
            assert (await quote_state(connection, second.quote_id))["is_pending"]

            with pytest.raises(QuoteError) as error:
                await confirm_quote(
                    connection,
                    **target,
                    readback_token=receipt.readback_token,
                    answer="yes",
                )
            assert error.value.code == "quote_not_pending"

            with pytest.raises(QuoteError) as error:
                await confirm_quote(
                    connection,
                    **context,
                    quote_id=second.quote_id,
                    readback_token=receipt.readback_token,
                    answer="yes",
                )
            assert error.value.code == "quote_not_read_back"

            with pytest.raises(asyncpg.UniqueViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE public.quotes SET is_pending=true WHERE id=$1",
                        first.quote_id,
                    )

            with pytest.raises(asyncpg.CheckViolationError):
                async with connection.transaction():
                    await connection.execute(
                        """
                        UPDATE public.quotes
                        SET confirmed_at=clock_timestamp() WHERE id=$1
                        """,
                        second.quote_id,
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["add", "update", "remove"])
def test_cart_edits_invalidate_confirmation(
    database_settings: Settings, operation: str
):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, item_id, large_id, quote = await prepare_pending_quote(connection)
            target = {**context, "quote_id": quote.quote_id}

            receipt = await complete_quote_readback(connection, **target)
            args = {
                **target,
                "readback_token": receipt.readback_token,
                "answer": "yes",
            }
            saved = await confirm_quote(connection, **args)

            if operation == "add":
                await add_cart_item(
                    connection,
                    **context,
                    expected_version=1,
                    item=AddCartItemRequest(
                        item_id=item_id,
                        option_ids=[large_id],
                    ),
                )
            elif operation == "update":
                await update_cart_item(
                    connection,
                    **context,
                    expected_version=1,
                    line_id=quote.items[0].line_id,
                    item=UpdateCartItemRequest(
                        quantity=2,
                        option_ids=[large_id],
                    ),
                )
            else:
                await remove_cart_item(
                    connection,
                    **context,
                    expected_version=1,
                    line_id=quote.items[0].line_id,
                )

            state = await quote_state(connection, quote.quote_id)
            assert state["is_pending"] is False
            assert state["confirmed_at"] == saved.confirmed_at
            assert (await get_cart(connection, **context)).version == 2

            with pytest.raises(QuoteError) as error:
                await confirm_quote(connection, **args)
            assert error.value.code == "quote_stale"

    asyncio.run(scenario())


def test_failed_changes_preserve_confirmation(database_settings: Settings, monkeypatch):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, _, _, quote = await prepare_pending_quote(connection)
            target = {**context, "quote_id": quote.quote_id}

            receipt = await complete_quote_readback(connection, **target)
            args = {
                **target,
                "readback_token": receipt.readback_token,
                "answer": "yes",
            }
            saved = await confirm_quote(connection, **args)

            # A rejected edit must restore both the version and pending quote.
            with pytest.raises(SelectionError):
                await update_cart_item(
                    connection,
                    **context,
                    expected_version=1,
                    line_id=quote.items[0].line_id,
                    item=UpdateCartItemRequest(quantity=2, option_ids=[]),
                )

            assert (await get_cart(connection, **context)).version == 1
            assert await confirm_quote(connection, **args) == saved

            # A failure after replacement must restore the previous quote.
            original = QuoteRepository.make_pending

            async def fail_after_replacing(self, quote_id, cart_id):
                await original(self, quote_id, cart_id)
                raise OSError("simulated replacement failure")

            with monkeypatch.context() as patch:
                patch.setattr(
                    QuoteRepository,
                    "make_pending",
                    fail_after_replacing,
                )
                with pytest.raises(OSError):
                    await create_quote(connection, **context, expected_version=1)

            assert await confirm_quote(connection, **args) == saved
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.quotes WHERE cart_id=$1",
                    context["cart_id"],
                )
                == 1
            )

            # A failure after writing confirmation must roll back that write.
            receipt = await complete_quote_readback(connection, **target)
            args["readback_token"] = receipt.readback_token
            original_confirmation = QuoteRepository.record_confirmation

            async def fail_after_confirmation(self, quote_id, confirmed_at):
                await original_confirmation(self, quote_id, confirmed_at)
                raise OSError("simulated confirmation failure")

            with monkeypatch.context() as patch:
                patch.setattr(
                    QuoteRepository,
                    "record_confirmation",
                    fail_after_confirmation,
                )
                with pytest.raises(OSError):
                    await confirm_quote(connection, **args)

            state = await quote_state(connection, quote.quote_id)
            assert state["confirmed_at"] is None
            assert await confirm_quote(connection, **args) is not None

    asyncio.run(scenario())


def test_confirmation_enforces_context_status_and_expiry(
    database_settings: Settings,
):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, _, _, quote = await prepare_pending_quote(connection)
            target = {**context, "quote_id": quote.quote_id}
            receipt = await complete_quote_readback(connection, **target)

            for operation in (complete_quote_readback, confirm_quote):
                extra = (
                    {}
                    if operation is complete_quote_readback
                    else {
                        "readback_token": receipt.readback_token,
                        "answer": "yes",
                    }
                )

                for wrong in (
                    {**target, "customer_id": uuid4()},
                    {**target, "cart_id": uuid4()},
                ):
                    with pytest.raises(CartError) as error:
                        await operation(connection, **wrong, **extra)
                    assert error.value.code == "cart_not_found"

                with pytest.raises(QuoteError) as error:
                    await operation(
                        connection,
                        **{**target, "quote_id": uuid4()},
                        **extra,
                    )
                assert error.value.code == "quote_not_found"

            await connection.execute(
                "UPDATE public.carts SET status='CHECKED_OUT' WHERE id=$1",
                context["cart_id"],
            )

            with pytest.raises(QuoteError) as error:
                await confirm_quote(
                    connection,
                    **target,
                    readback_token=receipt.readback_token,
                    answer="yes",
                )
            assert error.value.code == "cart_not_quotable"

            await connection.execute(
                "UPDATE public.carts SET status='BUILDING' WHERE id=$1",
                context["cart_id"],
            )

            # Insert an expired test snapshot without sleeping.
            expired = quote.model_copy(
                update={
                    "quote_id": uuid4(),
                    "created_at": quote.created_at - timedelta(hours=1),
                    "expires_at": quote.expires_at - timedelta(hours=1),
                    "pickup_at": quote.pickup_at - timedelta(hours=1),
                }
            )

            repository = QuoteRepository(connection)
            await repository.insert(expired)
            await repository.make_pending(expired.quote_id, context["cart_id"])

            token = uuid4()
            await repository.record_readback(
                expired.quote_id,
                token,
                expired.created_at + timedelta(seconds=1),
            )

            target["quote_id"] = expired.quote_id

            for operation in (complete_quote_readback, confirm_quote):
                extra = (
                    {}
                    if operation is complete_quote_readback
                    else {"readback_token": token, "answer": "yes"}
                )

                with pytest.raises(QuoteError) as error:
                    await operation(connection, **target, **extra)
                assert error.value.code == "quote_expired"

            state = await quote_state(connection, expired.quote_id)
            assert state["confirmed_at"] is None

    asyncio.run(scenario())


def test_confirmation_waits_for_cart_edit(database_settings: Settings):
    async def scenario():
        async with committed_cart_data(database_settings) as d:
            context = {
                "cart_id": d.cart_id,
                "customer_id": d.customer_id,
            }

            async with database_app(database_settings) as app:
                async with (
                    app.state.db_pool.acquire(timeout=5) as first,
                    app.state.db_pool.acquire(timeout=5) as second,
                ):
                    # A test-only quote avoids changing shared settings.
                    async with first.transaction():
                        cart = await get_cart(first, **context)
                        quote = build_quote(
                            cart,
                            RestaurantConfiguration(
                                restaurant_name="Confirmation Test",
                                ordering_enabled=True,
                                preparation_minutes=20,
                                tax_rate_bps=1000,
                                hours=[
                                    OpeningPeriod(
                                        weekday=day,
                                        opens_minute=0,
                                        closes_minute=1440,
                                    )
                                    for day in range(7)
                                ],
                            ),
                            await first.fetchval("SELECT clock_timestamp()"),
                        )

                        repository = QuoteRepository(first)
                        await repository.insert(quote)
                        await repository.make_pending(quote.quote_id, d.cart_id)

                    target = {**context, "quote_id": quote.quote_id}
                    receipt = await complete_quote_readback(first, **target)

                    pending = None
                    try:
                        async with first.transaction():
                            await update_cart_item(
                                first,
                                **context,
                                expected_version=1,
                                line_id=d.line_id,
                                item=UpdateCartItemRequest(
                                    quantity=2,
                                    option_ids=[d.regular_id],
                                ),
                            )

                            pending = asyncio.create_task(
                                confirm_quote(
                                    second,
                                    **target,
                                    readback_token=receipt.readback_token,
                                    answer="yes",
                                )
                            )

                            async with asyncio.timeout(3):
                                while not await first.fetchval(
                                    """
                                    SELECT $1::integer =
                                        ANY(pg_blocking_pids($2::integer))
                                    """,
                                    first.get_server_pid(),
                                    second.get_server_pid(),
                                ):
                                    if pending.done():
                                        pending.result()
                                        pytest.fail(
                                            "Confirmation did not wait "
                                            "for the cart lock"
                                        )
                                    await asyncio.sleep(0.01)

                        with pytest.raises(QuoteError) as error:
                            await asyncio.wait_for(pending, timeout=5)

                        assert error.value.code == "quote_stale"
                        state = await quote_state(first, quote.quote_id)
                        assert state["confirmed_at"] is None
                    finally:
                        if pending is not None:
                            if not pending.done():
                                pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())


async def confirm_test_quote(connection, context, quote):
    target = {**context, "quote_id": quote.quote_id}
    receipt = await complete_quote_readback(connection, **target)

    confirmation = await confirm_quote(
        connection,
        **target,
        readback_token=receipt.readback_token,
        answer="yes",
    )
    assert confirmation is not None
    return confirmation


def test_order_copies_confirmed_snapshot(database_settings: Settings):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, item_id, large_id, _ = await prepare_pending_quote(connection)

            # Include two separate lines, one without modifiers.
            await connection.execute(
                """
                UPDATE public.modifier_groups
                SET min_selections=0 WHERE menu_item_id=$1
                """,
                item_id,
            )
            await add_cart_item(
                connection,
                **context,
                expected_version=1,
                item=AddCartItemRequest(
                    item_id=item_id,
                    quantity=2,
                    option_ids=[],
                ),
            )

            quote = await create_quote(connection, **context, expected_version=2)
            confirmation = await confirm_test_quote(connection, context, quote)
            order = await place_order(connection, **context, quote_id=quote.quote_id)

            assert order.order_number > 0
            assert order.cart_id == quote.cart_id
            assert order.quote_id == quote.quote_id
            assert order.cart_version == 2
            assert order.status == "PLACED"
            assert order.payment_status == "UNPAID"
            assert order.items == quote.items
            assert (
                order.subtotal_cents,
                order.tax_cents,
                order.total_cents,
            ) == (4497, 450, 4947)
            assert order.tax_rate_bps == quote.tax_rate_bps == 1000
            assert order.pickup_at == quote.pickup_at
            assert order.confirmed_at == confirmation.confirmed_at
            assert order.confirmed_at <= order.placed_at < quote.expires_at

            cart = await get_cart(connection, **context)
            assert cart.status == "CHECKED_OUT"
            assert cart.version == 2

            state = await quote_state(connection, quote.quote_id)
            assert state["is_pending"] is False

            event = await connection.fetchrow(
                """
                SELECT event_type,
                       data->>'payment_status' AS payment,
                       occurred_at
                FROM public.order_events
                WHERE order_id=$1
                """,
                order.order_id,
            )
            assert event["event_type"] == "ORDER_PLACED"
            assert event["payment"] == "UNPAID"
            assert event["occurred_at"] == order.placed_at

            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.order_events WHERE order_id=$1",
                    order.order_id,
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.order_items WHERE order_id=$1",
                    order.order_id,
                )
                == 2
            )

            with pytest.raises(OrderError) as error:
                await get_order(
                    connection,
                    order_id=order.order_id,
                    customer_id=uuid4(),
                )
            assert error.value.code == "order_not_found"

            with pytest.raises(CartError) as error:
                await remove_cart_item(
                    connection,
                    **context,
                    expected_version=2,
                    line_id=quote.items[0].line_id,
                )
            assert error.value.code == "cart_not_editable"

            # Lesson 4 will change this retry into a saved-order response.
            assert (
                await place_order(
                    connection,
                    **context,
                    quote_id=quote.quote_id,
                )
                == order
            )

            # The database also rejects a second order for this cart.
            with pytest.raises(asyncpg.UniqueViolationError):
                async with connection.transaction():
                    await OrderRepository(connection).insert(
                        quote,
                        context["customer_id"],
                        confirmation.confirmed_at,
                        order.placed_at,
                    )

            await connection.execute(
                """
                UPDATE public.menu_items
                SET name='New bowl', price_cents=1999 WHERE id=$1
                """,
                item_id,
            )
            await connection.execute(
                """
                UPDATE public.modifier_options
                SET name='XL', price_delta_cents=500, is_available=false
                WHERE id=$1
                """,
                large_id,
            )
            await connection.execute(
                """
                UPDATE public.restaurant_settings
                SET tax_rate_bps=500 WHERE id=1
                """
            )

            # Test-only removal proves the order has its own snapshots.
            await connection.execute(
                "DELETE FROM public.cart_items WHERE cart_id=$1",
                context["cart_id"],
            )

            loaded = await get_order(
                connection,
                order_id=order.order_id,
                customer_id=context["customer_id"],
            )
            assert loaded == order

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case,code",
    [
        ("unconfirmed", "quote_not_confirmed"),
        ("reread", "quote_not_confirmed"),
        ("declined", "quote_not_pending"),
        ("replaced", "quote_not_pending"),
        ("edited", "quote_stale"),
        ("expired", "quote_expired"),
        ("wrong_customer", "cart_not_found"),
        ("wrong_cart", "cart_not_found"),
        ("missing_quote", "quote_not_found"),
        ("different_cart", "quote_not_found"),
    ],
)
def test_order_requires_current_confirmation(database_settings: Settings, case, code):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, _, large_id, quote = await prepare_pending_quote(connection)
            target = {**context, "quote_id": quote.quote_id}

            if case != "unconfirmed":
                await confirm_test_quote(connection, context, quote)

            if case == "reread":
                await complete_quote_readback(connection, **target)

            elif case == "declined":
                state = await quote_state(connection, quote.quote_id)
                await confirm_quote(
                    connection,
                    **target,
                    readback_token=state["readback_token"],
                    answer="no",
                )

            elif case == "replaced":
                await create_quote(connection, **context, expected_version=1)

            elif case == "edited":
                await update_cart_item(
                    connection,
                    **context,
                    expected_version=1,
                    line_id=quote.items[0].line_id,
                    item=UpdateCartItemRequest(
                        quantity=2,
                        option_ids=[large_id],
                    ),
                )

            elif case == "expired":
                # Move the whole test confirmation window into the past.
                await connection.execute(
                    """
                    UPDATE public.quotes SET
                        created_at=created_at-interval '1 hour',
                        expires_at=expires_at-interval '1 hour',
                        pickup_at=pickup_at-interval '1 hour',
                        readback_completed_at=
                            readback_completed_at-interval '1 hour',
                        confirmed_at=confirmed_at-interval '1 hour'
                    WHERE id=$1
                    """,
                    quote.quote_id,
                )

            elif case == "wrong_customer":
                target["customer_id"] = uuid4()

            elif case == "wrong_cart":
                target["cart_id"] = uuid4()

            elif case == "missing_quote":
                target["quote_id"] = uuid4()

            elif case == "different_cart":
                target["cart_id"] = await connection.fetchval(
                    """
                    INSERT INTO public.carts (customer_id)
                    VALUES ($1) RETURNING id
                    """,
                    context["customer_id"],
                )

            with pytest.raises((CartError, QuoteError)) as error:
                await place_order(connection, **target)
            assert error.value.code == code

            assert await connection.fetchval("SELECT count(*) FROM public.orders") == 0
            assert (
                await connection.fetchval("SELECT count(*) FROM public.order_events")
                == 0
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stage",
    ["insert_items", "append_placed_event"],
)
def test_order_failure_rolls_back_all_writes(
    database_settings: Settings, monkeypatch, stage
):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, _, _, quote = await prepare_pending_quote(connection)
            confirmation = await confirm_test_quote(connection, context, quote)
            original = getattr(OrderRepository, stage)

            async def fail_after_write(self, *args):
                await original(self, *args)
                raise OSError("simulated order failure")

            with monkeypatch.context() as patch:
                patch.setattr(OrderRepository, stage, fail_after_write)

                with pytest.raises(OSError):
                    await place_order(
                        connection,
                        **context,
                        quote_id=quote.quote_id,
                    )

            for table in (
                "orders",
                "order_items",
                "order_item_modifiers",
                "order_events",
            ):
                # Fixed test table names, never user input.
                assert (
                    await connection.fetchval(f"SELECT count(*) FROM public.{table}")
                    == 0
                )

            cart = await get_cart(connection, **context)
            assert cart.status == "BUILDING"
            assert cart.version == 1

            state = await quote_state(connection, quote.quote_id)
            assert state["is_pending"] is True
            assert state["confirmed_at"] == confirmation.confirmed_at

            # The connection and confirmation remain usable after rollback.
            order = await place_order(
                connection,
                **context,
                quote_id=quote.quote_id,
            )
            assert order.status == "PLACED"

    asyncio.run(scenario())


def test_committed_order_survives_new_app(database_settings: Settings):
    async def scenario():
        async with confirmed_checkout_data(database_settings) as d:
            async with database_app(database_settings) as first_app:
                async with first_app.state.db_pool.acquire(timeout=5) as connection:
                    saved = await place_order(connection, **d.target)
                    assert isinstance(saved, OrderResponse)

            async with database_app(database_settings) as second_app:
                async with second_app.state.db_pool.acquire(timeout=5) as connection:
                    loaded = await get_order(
                        connection,
                        order_id=saved.order_id,
                        customer_id=d.customer_id,
                    )
                    assert loaded == saved
                    assert await place_order(connection, **d.target) == saved

                    cart = await get_cart(connection, **d.context)
                    assert cart.status == "CHECKED_OUT"

                    assert (
                        await connection.fetchval(
                            """
                            SELECT count(*) FROM public.order_events
                            WHERE order_id=$1
                            """,
                            saved.order_id,
                        )
                        == 1
                    )

    asyncio.run(scenario())


async def expire_test_quote(connection, quote_id):
    # Test-only timestamp adjustment; no real waiting is needed.
    await connection.execute(
        """
        UPDATE public.quotes SET
            created_at=created_at-interval '1 hour',
            expires_at=expires_at-interval '1 hour',
            pickup_at=pickup_at-interval '1 hour',
            readback_completed_at=readback_completed_at-interval '1 hour',
            confirmed_at=confirmed_at-interval '1 hour'
        WHERE id=$1
        """,
        quote_id,
    )


def test_checkout_retry_recovers_saved_order(database_settings: Settings):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, item_id, _, quote = await prepare_pending_quote(connection)
            await confirm_test_quote(connection, context, quote)
            target = {**context, "quote_id": quote.quote_id}

            saved = await place_order(connection, **target)
            assert isinstance(saved, OrderResponse)

            await expire_test_quote(connection, quote.quote_id)
            await connection.execute(
                """
                UPDATE public.restaurant_settings
                SET ordering_enabled=false, tax_rate_bps=500
                WHERE id=1
                """
            )
            await connection.execute(
                """
                UPDATE public.menu_items
                SET price_cents=9999, is_available=false
                WHERE id=$1
                """,
                item_id,
            )

            assert await place_order(connection, **target) == saved

            with pytest.raises(OrderError) as error:
                await place_order(
                    connection,
                    **{**target, "quote_id": uuid4()},
                )
            assert error.value.code == "order_quote_mismatch"

            with pytest.raises(CartError) as error:
                await place_order(
                    connection,
                    **{**target, "customer_id": uuid4()},
                )
            assert error.value.code == "cart_not_found"

            assert await connection.fetchval("SELECT count(*) FROM public.orders") == 1
            assert (
                await connection.fetchval("SELECT count(*) FROM public.order_events")
                == 1
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change,code,has_quote",
    [
        ("item_price", "quote_changed", True),
        ("option_price", "quote_changed", True),
        ("item_name", "quote_changed", True),
        ("option_name", "quote_changed", True),
        ("tax", "quote_changed", True),
        ("preparation", "quote_changed", True),
        ("item_unavailable", "invalid_cart", False),
        ("option_unavailable", "invalid_cart", False),
        ("required_group", "invalid_cart", False),
        ("group_limits", "invalid_cart", False),
        ("paused", "ordering_paused", False),
        ("closed", "restaurant_closed", False),
        ("tax_missing", "tax_not_configured", False),
    ],
)
def test_checkout_revalidates_and_requires_review(
    database_settings, change, code, has_quote
):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, item_id, large_id, quote = await prepare_pending_quote(connection)
            await confirm_test_quote(connection, context, quote)

            changes = {
                "item_price": (
                    "UPDATE public.menu_items SET price_cents=1499 WHERE id=$1",
                    [item_id],
                ),
                "option_price": (
                    "UPDATE public.modifier_options "
                    "SET price_delta_cents=400 WHERE id=$1",
                    [large_id],
                ),
                "item_name": (
                    "UPDATE public.menu_items SET name='New bowl' WHERE id=$1",
                    [item_id],
                ),
                "option_name": (
                    "UPDATE public.modifier_options SET name='XL' WHERE id=$1",
                    [large_id],
                ),
                "tax": (
                    "UPDATE public.restaurant_settings SET tax_rate_bps=500 WHERE id=1",
                    [],
                ),
                "preparation": (
                    "UPDATE public.restaurant_settings "
                    "SET preparation_minutes=60 WHERE id=1",
                    [],
                ),
                "item_unavailable": (
                    "UPDATE public.menu_items SET is_available=false WHERE id=$1",
                    [item_id],
                ),
                "option_unavailable": (
                    "UPDATE public.modifier_options SET is_available=false WHERE id=$1",
                    [large_id],
                ),
                "required_group": (
                    "INSERT INTO public.modifier_groups "
                    "(menu_item_id,name,min_selections,max_selections) "
                    "VALUES ($1,'New requirement',1,1)",
                    [item_id],
                ),
                "group_limits": (
                    "UPDATE public.modifier_groups "
                    "SET min_selections=2,max_selections=2 "
                    "WHERE menu_item_id=$1",
                    [item_id],
                ),
                "paused": (
                    "UPDATE public.restaurant_settings "
                    "SET ordering_enabled=false WHERE id=1",
                    [],
                ),
                "closed": (
                    "DELETE FROM public.opening_hours",
                    [],
                ),
                "tax_missing": (
                    "UPDATE public.restaurant_settings "
                    "SET tax_rate_bps=NULL WHERE id=1",
                    [],
                ),
            }

            sql, arguments = changes[change]
            await connection.execute(sql, *arguments)

            result = await place_order(
                connection,
                **context,
                quote_id=quote.quote_id,
            )

            assert isinstance(result, CheckoutNeedsReview)
            assert result.code == code
            assert (result.quote is not None) is has_quote

            old_state = await quote_state(connection, quote.quote_id)
            assert old_state["is_pending"] is False

            cart = await get_cart(connection, **context)
            assert cart.status == "BUILDING"
            assert cart.version == 1

            assert await connection.fetchval("SELECT count(*) FROM public.orders") == 0

            if result.quote is not None:
                replacement = result.quote
                assert replacement.quote_id != quote.quote_id
                assert replacement.cart_version == quote.cart_version

                state = await quote_state(connection, replacement.quote_id)
                assert state["is_pending"]
                assert state["confirmed_at"] is None

                with pytest.raises(QuoteError) as error:
                    await place_order(
                        connection,
                        **context,
                        quote_id=replacement.quote_id,
                    )
                assert error.value.code == "quote_not_confirmed"

                await confirm_test_quote(
                    connection,
                    context,
                    replacement,
                )
                order = await place_order(
                    connection,
                    **context,
                    quote_id=replacement.quote_id,
                )

                assert isinstance(order, OrderResponse)
                assert order.items == replacement.items
                assert order.total_cents == replacement.total_cents
                assert order.pickup_at == replacement.pickup_at

            elif change == "paused":
                await connection.execute(
                    """
                    UPDATE public.restaurant_settings
                    SET ordering_enabled=true WHERE id=1
                    """
                )
                with pytest.raises(QuoteError) as error:
                    await place_order(
                        connection,
                        **context,
                        quote_id=quote.quote_id,
                    )
                assert error.value.code == "quote_not_pending"

    asyncio.run(scenario())


def test_failed_requote_rolls_back_checkout_changes(database_settings, monkeypatch):
    async def scenario():
        async with isolated_data(database_settings) as connection:
            context, item_id, _, quote = await prepare_pending_quote(connection)
            confirmation = await confirm_test_quote(connection, context, quote)

            await connection.execute(
                "UPDATE public.menu_items SET price_cents=1499 WHERE id=$1",
                item_id,
            )

            original = QuoteRepository.make_pending

            async def fail_after_replacement(self, *args):
                await original(self, *args)
                raise OSError("simulated requote failure")

            with monkeypatch.context() as patch:
                patch.setattr(
                    QuoteRepository,
                    "make_pending",
                    fail_after_replacement,
                )
                with pytest.raises(OSError):
                    await place_order(
                        connection,
                        **context,
                        quote_id=quote.quote_id,
                    )

            state = await quote_state(connection, quote.quote_id)
            assert state["is_pending"]
            assert state["confirmed_at"] == confirmation.confirmed_at

            assert await connection.fetchval("SELECT count(*) FROM public.quotes") == 1
            assert await connection.fetchval("SELECT count(*) FROM public.orders") == 0

            cart = await get_cart(connection, **context)
            assert cart.status == "BUILDING"

            result = await place_order(
                connection,
                **context,
                quote_id=quote.quote_id,
            )
            assert isinstance(result, CheckoutNeedsReview)
            assert result.quote is not None

    asyncio.run(scenario())


async def wait_for_database_block(blocker, waiter, task):
    async with asyncio.timeout(3):
        while not await blocker.fetchval(
            "SELECT $1::integer = ANY(pg_blocking_pids($2::integer))",
            blocker.get_server_pid(),
            waiter.get_server_pid(),
        ):
            if task.done():
                task.result()
                pytest.fail("Expected the second operation to wait for a database lock")

            await asyncio.sleep(0.01)


async def settle_test_task(task):
    if task is not None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_concurrent_checkout_returns_one_order(database_settings):
    async def scenario():
        async with confirmed_checkout_data(database_settings) as d:
            async with database_app(database_settings) as app:
                async with (
                    app.state.db_pool.acquire(timeout=5) as first,
                    app.state.db_pool.acquire(timeout=5) as second,
                ):
                    pending = None
                    try:
                        async with first.transaction():
                            saved = await place_order(first, **d.target)
                            assert isinstance(saved, OrderResponse)

                            pending = asyncio.create_task(
                                place_order(second, **d.target)
                            )
                            await wait_for_database_block(first, second, pending)

                        assert await asyncio.wait_for(pending, timeout=5) == saved
                        assert (
                            await first.fetchval(
                                """
                                SELECT count(*) FROM public.orders
                                WHERE cart_id=$1
                                """,
                                d.cart_id,
                            )
                            == 1
                        )
                        assert (
                            await first.fetchval(
                                """
                                SELECT count(*) FROM public.order_events
                                WHERE order_id=$1
                                """,
                                saved.order_id,
                            )
                            == 1
                        )
                    finally:
                        await settle_test_task(pending)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["price", "expiry"])
def test_checkout_checks_again_after_catalog_wait(database_settings, change):
    async def scenario():
        async with confirmed_checkout_data(database_settings) as d:
            async with database_app(database_settings) as app:
                async with (
                    app.state.db_pool.acquire(timeout=5) as first,
                    app.state.db_pool.acquire(timeout=5) as second,
                ):
                    pending = None
                    try:
                        async with first.transaction():
                            await first.execute(
                                "LOCK TABLE public.menu_items IN ROW EXCLUSIVE MODE"
                            )

                            pending = asyncio.create_task(
                                place_order(second, **d.target)
                            )
                            await wait_for_database_block(first, second, pending)

                            if change == "price":
                                await MenuAdminRepository(first).update_item(
                                    d.item_id, 1499, None
                                )
                            else:
                                await expire_test_quote(first, d.quote.quote_id)

                        if change == "price":
                            result = await asyncio.wait_for(pending, timeout=5)
                            assert isinstance(result, CheckoutNeedsReview)
                            assert result.quote.items[0].unit_price_cents == 1499
                        else:
                            with pytest.raises(QuoteError) as error:
                                await asyncio.wait_for(pending, timeout=5)
                            assert error.value.code == "quote_expired"

                        assert (
                            await first.fetchval(
                                """
                                SELECT count(*) FROM public.orders
                                WHERE cart_id=$1
                                """,
                                d.cart_id,
                            )
                            == 0
                        )
                    finally:
                        await settle_test_task(pending)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["price", "required_group"])
def test_catalog_changes_wait_for_checkout_commit(database_settings, change):
    async def scenario():
        async with confirmed_checkout_data(database_settings) as d:
            async with database_app(database_settings) as app:
                async with (
                    app.state.db_pool.acquire(timeout=5) as first,
                    app.state.db_pool.acquire(timeout=5) as second,
                ):
                    pending = None
                    try:
                        async with first.transaction():
                            saved = await place_order(first, **d.target)
                            assert isinstance(saved, OrderResponse)

                            if change == "price":
                                operation = MenuAdminRepository(second).update_item(
                                    d.item_id, 1499, None
                                )
                            else:
                                operation = second.execute(
                                    """
                                    INSERT INTO public.modifier_groups (
                                        menu_item_id, name,
                                        min_selections, max_selections
                                    )
                                    VALUES ($1,'New requirement',1,1)
                                    """,
                                    d.item_id,
                                )

                            pending = asyncio.create_task(operation)
                            await wait_for_database_block(first, second, pending)

                        await asyncio.wait_for(pending, timeout=5)

                        loaded = await get_order(
                            first,
                            order_id=saved.order_id,
                            customer_id=d.customer_id,
                        )
                        assert loaded == saved
                    finally:
                        await settle_test_task(pending)

    asyncio.run(scenario())
