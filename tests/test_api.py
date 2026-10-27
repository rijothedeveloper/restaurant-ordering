from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    pool = SimpleNamespace(close=AsyncMock())
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
