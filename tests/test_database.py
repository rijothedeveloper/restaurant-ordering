import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
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
