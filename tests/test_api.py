from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.schemas.menu import MenuResponse


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
        "group_id": None,
        "group_name": None,
        "min_selections": None,
        "max_selections": None,
        "option_id": None,
        "option_name": None,
        "price_delta_cents": None,
        "option_is_available": None,
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
            "modifier_groups": [],
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


def test_menu_returns_modifier_groups(
    settings: Settings, pool_factory: AsyncMock
) -> None:
    item_id, size_id, extras_id = uuid4(), uuid4(), uuid4()

    base = {
        "restaurant_name": "Test Kitchen",
        "ordering_enabled": False,
        "category_id": uuid4(),
        "category_name": "Mains",
        "item_id": item_id,
        "item_name": "Test Bowl",
        "description": "Rice bowl",
        "price_cents": 1399,
        "is_available": True,
        "group_id": None,
        "group_name": None,
        "min_selections": None,
        "max_selections": None,
        "option_id": None,
        "option_name": None,
        "price_delta_cents": None,
        "option_is_available": None,
    }

    rows = []

    for group_id, group_name, minimum, maximum, options in [
        (size_id, "Size", 1, 1, [("Regular", 0, True), ("Large", 300, True)]),
        (extras_id, "Extras", 0, 2, [("Chicken", 400, True), ("Cheese", 150, False)]),
    ]:
        for name, price, available in options:
            rows.append(
                dict(
                    base,
                    group_id=group_id,
                    group_name=group_name,
                    min_selections=minimum,
                    max_selections=maximum,
                    option_id=uuid4(),
                    option_name=name,
                    price_delta_cents=price,
                    option_is_available=available,
                )
            )

    rows.append(
        dict(
            base,
            group_id=uuid4(),
            group_name="Empty",
            min_selections=0,
            max_selections=1,
        )
    )
    rows.append(dict(base, item_id=uuid4(), item_name="Plain Bowl"))

    connection = pool_factory.return_value.acquire.return_value.__aenter__.return_value
    connection.fetch = AsyncMock(return_value=rows)

    with TestClient(create_app(settings)) as client:
        response = client.get("/menu")

    assert response.status_code == 200

    items = response.json()["categories"][0]["items"]
    assert [item["name"] for item in items] == ["Test Bowl", "Plain Bowl"]

    groups = items[0]["modifier_groups"]
    assert [group["name"] for group in groups] == ["Size", "Extras", "Empty"]
    assert [(g["min_selections"], g["max_selections"]) for g in groups] == [
        (1, 1),
        (0, 2),
        (0, 1),
    ]

    assert [o["name"] for o in groups[0]["options"]] == ["Regular", "Large"]
    assert [o["price_delta_cents"] for o in groups[0]["options"]] == [0, 300]

    assert len(groups[1]["options"]) == 2
    assert groups[1]["options"][1]["is_available"] is False
    assert groups[2]["options"] == []
    assert items[1]["modifier_groups"] == []

    connection.fetch.assert_awaited_once()


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("valid", 200, None),
        ("required", 422, "too_few_options"),
        ("missing_item", 404, "item_not_found"),
        ("duplicate", 422, "duplicate_option"),
        ("unavailable", 422, "option_unavailable"),
        ("client_price", 422, None),
        ("bad_uuid", 422, None),
        ("database_failure", 503, None),
        ("missing_settings", 503, None),
    ],
)
def test_selection_endpoint(
    settings: Settings,
    pool_factory: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    status: int,
    code: str | None,
) -> None:
    item_id, option_id = uuid4(), uuid4()

    menu = MenuResponse.model_validate(
        {
            "restaurant_name": "Test Kitchen",
            "ordering_enabled": False,
            "categories": [
                {
                    "id": uuid4(),
                    "name": "Mains",
                    "items": [
                        {
                            "id": item_id,
                            "name": "Bowl",
                            "description": "",
                            "price_cents": 1399,
                            "is_available": True,
                            "modifier_groups": [
                                {
                                    "id": uuid4(),
                                    "name": "Size",
                                    "min_selections": 1,
                                    "max_selections": 1,
                                    "options": [
                                        {
                                            "id": option_id,
                                            "name": "Large",
                                            "price_delta_cents": 300,
                                            "is_available": case != "unavailable",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    fetch = AsyncMock(return_value=None if case == "missing_settings" else menu)

    if case == "database_failure":
        fetch.side_effect = OSError("private connection details")

    monkeypatch.setattr("app.api.menu.MenuRepository.get", fetch)

    body = {
        "item_id": str(item_id),
        "option_ids": [str(option_id)],
    }

    if case == "required":
        body["option_ids"] = []
    elif case == "missing_item":
        body["item_id"] = str(uuid4())
    elif case == "duplicate":
        body["option_ids"] = [str(option_id), str(option_id)]
    elif case == "client_price":
        body["unit_price_cents"] = 0
    elif case == "bad_uuid":
        body["item_id"] = "not-a-uuid"

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/menu/validate-selection",
            json=body,
        )

    assert response.status_code == status

    if case in {"client_price", "bad_uuid"}:
        fetch.assert_not_awaited()
        assert isinstance(response.json()["detail"], list)
    else:
        assert response.headers["cache-control"] == "no-store"

        if status == 200:
            assert response.json()["unit_price_cents"] == 1699
            assert response.json()["options"][0]["id"] == str(option_id)
        elif code is not None:
            assert response.json()["detail"]["code"] == code
        elif case == "database_failure":
            assert response.json() == {"detail": "Menu temporarily unavailable"}
        else:
            assert response.json() == {"detail": "Restaurant is not configured"}
