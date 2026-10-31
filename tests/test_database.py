import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.repositories.menu import MenuRepository
from app.repositories.restaurant_config import RestaurantConfigurationRepository

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
                transport=ASGITransport(app=app), base_url="http://test"
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
            assert [c.name for c in menu.categories] == [
                "Mains",
                "Drinks",
                "Empty",
            ]
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
                assert saved[0]["price_cents"] == 1399
                assert len(saved[0]["modifier_groups"]) == 1

                group = saved[0]["modifier_groups"][0]
                assert group["id"] == str(group_id)
                assert (group["min_selections"], group["max_selections"]) == (1, 1)
                assert group["options"] == [
                    {
                        "id": str(option_id),
                        "name": "Large",
                        "price_delta_cents": 300,
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
