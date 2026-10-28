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
