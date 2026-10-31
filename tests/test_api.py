from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_name="Restaurant API tests",
        environment="test",
        debug=False,
        database_url=SecretStr("postgres://unused:unused@127.0.0.1:1/restaurant_test"),
        db_pool_min_size=1,
        db_pool_max_size=5,
    )


@pytest.fixture
def pool_factory(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    connection = SimpleNamespace(fetchval=AsyncMock(return_value=1))
    acquire = MagicMock()
    acquire.return_value.__aenter__.return_value = connection
    pool = SimpleNamespace(close=AsyncMock(), acquire=acquire)
    factory = AsyncMock(return_value=pool)
    monkeypatch.setattr("app.db.pool.asyncpg.create_pool", factory)
    return factory


def test_health_returns_ok(settings: Settings, pool_factory: AsyncMock) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_returns_service_info(settings: Settings, pool_factory: AsyncMock) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "service": "restaurant-ordering",
        "message": "API is running",
    }


def test_pool_opens_and_closes(settings: Settings, pool_factory: AsyncMock) -> None:
    app = create_app(settings)
    pool = pool_factory.return_value
    pool_factory.assert_not_called()

    with TestClient(app):
        pool_factory.assert_awaited_once()
        assert app.state.db_pool is pool
        pool.close.assert_not_awaited()

    pool.close.assert_awaited_once()
    assert app.state.db_pool is None


def test_database_failure_prevents_startup(
    settings: Settings, pool_factory: AsyncMock
) -> None:
    pool_factory.side_effect = OSError("Database unavailable")
    app = create_app(settings)

    with pytest.raises(OSError, match="Database unavailable"):
        with TestClient(app):
            pytest.fail("Startup should have failed")


def test_ready_returns_200(settings: Settings, pool_factory: AsyncMock) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("internal timeout"),
        OSError("internal network detail"),
        asyncpg.PostgresError("internal database detail"),
        asyncpg.InterfaceError("internal connection detail"),
    ],
)
def test_ready_failure_preserves_liveness(
    settings: Settings, pool_factory: AsyncMock, error: Exception
) -> None:
    pool = pool_factory.return_value
    connection = pool.acquire.return_value.__aenter__.return_value
    connection.fetchval.side_effect = error

    with TestClient(create_app(settings)) as client:
        ready_response = client.get("/ready")
        health_response = client.get("/health")

    assert ready_response.status_code == 503
    assert ready_response.json() == {"status": "not_ready"}
    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok"}


def test_ready_handles_pool_exhaustion(
    settings: Settings, pool_factory: AsyncMock
) -> None:
    pool = pool_factory.return_value
    pool.acquire.return_value.__aenter__.side_effect = TimeoutError()

    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


@pytest.mark.parametrize("empty_menu", [False, True])
def test_menu_returns_single_restaurant(
    settings: Settings, pool_factory: AsyncMock, empty_menu: bool
) -> None:
    category_id, item_id = uuid4(), uuid4()

    empty_row = {
        "restaurant_name": "Test Kitchen",
        "ordering_enabled": False,
        "category_id": None,
        "category_name": None,
        "item_id": None,
        "item_name": None,
        "description": None,
        "price_cents": None,
        "is_available": None,
    }

    item_row = dict(
        empty_row,
        category_id=category_id,
        category_name="Mains",
        item_id=item_id,
        item_name="Test Bowl",
        description="Rice bowl",
        price_cents=1399,
        is_available=False,
    )

    rows = (
        [empty_row]
        if empty_menu
        else [
            item_row,
            dict(item_row, item_id=uuid4(), item_name="Second Bowl"),
            dict(empty_row, category_id=uuid4(), category_name="Drinks"),
        ]
    )

    connection = pool_factory.return_value.acquire.return_value.__aenter__.return_value
    connection.fetch = AsyncMock(return_value=rows)

    with TestClient(create_app(settings)) as client:
        response = client.get("/menu")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"

    body = response.json()
    assert body["restaurant_name"] == "Test Kitchen"
    assert body["currency"] == "USD"
    assert body["ordering_enabled"] is False
    assert "restaurant_id" not in body and "location_id" not in body

    if empty_menu:
        assert body["categories"] == []
    else:
        assert [c["name"] for c in body["categories"]] == ["Mains", "Drinks"]
        assert len(body["categories"][0]["items"]) == 2
        assert body["categories"][0]["items"][0] == {
            "id": str(item_id),
            "name": "Test Bowl",
            "description": "Rice bowl",
            "price_cents": 1399,
            "is_available": False,
        }
        assert body["categories"][1]["items"] == []


def test_menu_requires_restaurant_settings(
    settings: Settings, pool_factory: AsyncMock
) -> None:
    connection = pool_factory.return_value.acquire.return_value.__aenter__.return_value
    connection.fetch = AsyncMock(return_value=[])

    with TestClient(create_app(settings)) as client:
        response = client.get("/menu")

    assert response.status_code == 503
    assert response.json() == {"detail": "Restaurant is not configured"}


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("private timeout detail"),
        OSError("private network detail"),
        asyncpg.PostgresConnectionError("private database detail"),
        asyncpg.InterfaceError("private connection detail"),
    ],
)
def test_menu_database_failure_is_503(
    settings: Settings, pool_factory: AsyncMock, error: Exception
) -> None:
    connection = pool_factory.return_value.acquire.return_value.__aenter__.return_value
    connection.fetch = AsyncMock(side_effect=error)

    with TestClient(create_app(settings)) as client:
        response = client.get("/menu")

    assert response.status_code == 503
    assert response.json() == {"detail": "Menu temporarily unavailable"}
    assert response.headers["cache-control"] == "no-store"
