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
from app.repositories.restaurant_config import RestaurantConfigurationRepository
from app.repositories.restaurants import RestaurantRepository

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


def test_committed_restaurant_survives_new_app(database_settings: Settings) -> None:
    async def scenario() -> None:
        created = None
        try:
            async with database_app(database_settings) as first_app:
                async with first_app.state.db_pool.acquire(timeout=5) as connection:
                    repository = RestaurantRepository(connection)
                    assert await repository.get_by_id(uuid4()) is None

                    async with connection.transaction():
                        created = await repository.create("Rijo's Test Kitchen")

                    assert created.name == "Rijo's Test Kitchen"
                    assert created.created_at.tzinfo is not None

            async with database_app(database_settings) as second_app:
                async with second_app.state.db_pool.acquire(timeout=5) as connection:
                    loaded = await RestaurantRepository(connection).get_by_id(
                        created.id
                    )
                    assert loaded == created
        finally:
            if created is not None:
                async with database_app(database_settings) as cleanup_app:
                    async with cleanup_app.state.db_pool.acquire(
                        timeout=5
                    ) as connection:
                        await connection.execute(
                            "DELETE FROM public.restaurants WHERE id = $1",
                            created.id,
                        )

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["", "   "])
def test_database_rejects_blank_names(database_settings: Settings, name: str) -> None:
    async def scenario() -> None:
        async with database_app(database_settings) as app:
            async with app.state.db_pool.acquire(timeout=5) as connection:
                repository = RestaurantRepository(connection)

                with pytest.raises(
                    asyncpg.CheckViolationError,
                    match="restaurants_name_not_blank",
                ):
                    async with connection.transaction():
                        await repository.create(name)

                assert await connection.fetchval("SELECT 1") == 1

    asyncio.run(scenario())


def test_location_configuration_and_scope(database_settings: Settings) -> None:
    async def scenario() -> None:
        async with database_app(database_settings) as app:
            async with app.state.db_pool.acquire(timeout=5) as connection:
                transaction = connection.transaction()
                await transaction.start()
                try:
                    restaurant = await RestaurantRepository(connection).create(
                        "Configuration test"
                    )
                    location_id = await connection.fetchval(
                        """
                        INSERT INTO public.locations (restaurant_id, name, timezone)
                        VALUES ($1, $2, $3)
                        RETURNING id
                        """,
                        restaurant.id,
                        "Main",
                        "America/Los_Angeles",
                    )
                    repository = RestaurantConfigurationRepository(connection)
                    empty = await repository.get(restaurant.id, location_id)
                    assert empty is not None
                    assert empty.hours == []
                    assert empty.ordering_enabled is False

                    await connection.execute(
                        """
                        INSERT INTO public.location_hours
                            (location_id, weekday, opens_minute, closes_minute)
                        VALUES ($1, 0, 660, 900), ($1, 0, 1020, 1260)
                        """,
                        location_id,
                    )
                    config = await repository.get(restaurant.id, location_id)
                    assert config is not None
                    assert config.currency == "USD"
                    assert config.preparation_minutes == 20
                    assert len(config.hours) == 2
                    assert await repository.get(uuid4(), location_id) is None
                    assert await repository.get(restaurant.id, uuid4()) is None

                    with pytest.raises(asyncpg.ExclusionViolationError):
                        async with connection.transaction():
                            await connection.execute(
                                """
                                INSERT INTO public.location_hours
                                    (location_id, weekday, opens_minute, closes_minute)
                                VALUES ($1, 0, 850, 1000)
                                """,
                                location_id,
                            )

                    await connection.execute(
                        """
                        INSERT INTO public.location_hours
                            (location_id, weekday, opens_minute, closes_minute)
                        VALUES ($1, 0, 900, 1020)
                        """,
                        location_id,
                    )
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with connection.transaction():
                            await connection.execute(
                                """
                                INSERT INTO public.location_hours
                                    (location_id, weekday, opens_minute, closes_minute)
                                VALUES ($1, 1, 100, 100)
                                """,
                                location_id,
                            )
                finally:
                    await transaction.rollback()

    asyncio.run(scenario())
