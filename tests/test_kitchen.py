from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from firebase_admin import auth as firebase_auth
from pydantic import ValidationError

from app.api import staff as staff_api
from app.auth import firebase_auth as auth_provider
from app.repositories.kitchen import KitchenRepository
from app.repositories.menu import MenuRepository
from app.repositories.menu_admin import MenuAdminRepository
from app.repositories.restaurant_config import (
    RestaurantConfigurationRepository,
)
from app.schemas.kitchen import AvailabilityChange, IntakeChange
from app.schemas.menu import MenuResponse
from app.schemas.restaurant_config import (
    OpeningPeriod,
    RestaurantConfiguration,
)
from app.services.intake import intake_block_reason, kitchen_is_online

NOW = datetime(2026, 11, 13, 20, tzinfo=UTC)
ITEM_ID = "00000000-0000-0000-0000-000000000001"

REQUESTS = [
    ("GET", "/staff/kitchen", None),
    ("POST", "/staff/kitchen/heartbeat", None),
    (
        "PATCH",
        "/staff/kitchen/intake",
        {"ordering_enabled": False},
    ),
    (
        "PATCH",
        f"/staff/menu/items/{ITEM_ID}/availability",
        {"is_available": False},
    ),
    (
        "PATCH",
        f"/staff/menu/options/{ITEM_ID}/availability",
        {"is_available": False},
    ),
]


def kitchen_config():
    return RestaurantConfiguration(
        restaurant_name="Test Kitchen",
        preparation_minutes=20,
        ordering_enabled=True,
        tax_rate_bps=1000,
        kitchen_last_seen_at=NOW,
        hours=[
            OpeningPeriod(
                weekday=day,
                opens_minute=0,
                closes_minute=1440,
            )
            for day in range(7)
        ],
    )


@pytest.mark.parametrize(
    "age,online",
    [
        (None, False),
        (-1, False),
        (0, True),
        (29.999, True),
        (30, False),
        (31, False),
    ],
)
def test_kitchen_heartbeat_deadline(age, online):
    config = kitchen_config()
    config.kitchen_last_seen_at = (
        None if age is None else NOW - timedelta(seconds=age)
    )

    assert kitchen_is_online(config, NOW) is online
    assert intake_block_reason(config, NOW) == (
        None if online else "kitchen_offline"
    )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("ordering_enabled", False, "ordering_paused"),
        ("tax_rate_bps", None, "tax_not_configured"),
        ("hours", [], "restaurant_closed"),
    ],
)
def test_heartbeat_does_not_override_other_rules(field, value, reason):
    config = kitchen_config().model_copy(update={field: value})

    assert kitchen_is_online(config, NOW)
    assert intake_block_reason(config, NOW) == reason


@pytest.mark.parametrize(
    "model,field",
    [
        (IntakeChange, "ordering_enabled"),
        (AvailabilityChange, "is_available"),
    ],
)
@pytest.mark.parametrize("value", ["true", 1, None])
def test_kitchen_commands_require_json_booleans(model, field, value):
    with pytest.raises(ValidationError):
        model.model_validate({field: value})


@pytest.fixture
def kitchen_client(monkeypatch):
    connection = SimpleNamespace(
        fetchval=AsyncMock(return_value=NOW)
    )
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(
        return_value=connection
    )
    pool.acquire.return_value.__aexit__ = AsyncMock(
        return_value=False
    )

    # Exercise the real require_staff dependency.
    # Replace only its external Firebase verification call.
    verify = Mock(
        return_value={"uid": "approved-staff", "staff": True}
    )
    monkeypatch.setattr(auth_provider, "verify_id_token", verify)

    config = AsyncMock(return_value=kitchen_config())
    monkeypatch.setattr(
        RestaurantConfigurationRepository, "get", config
    )
    monkeypatch.setattr(
        MenuRepository,
        "get",
        AsyncMock(
            return_value=MenuResponse(
                restaurant_name="Test Kitchen",
                ordering_enabled=True,
                categories=[],
            )
        ),
    )

    writes = {}
    for cls, method in [
        (KitchenRepository, "heartbeat"),
        (KitchenRepository, "set_ordering_enabled"),
        (MenuAdminRepository, "update_item"),
        (MenuAdminRepository, "update_option"),
    ]:
        writes[method] = AsyncMock(return_value=True)
        monkeypatch.setattr(cls, method, writes[method])

    app = FastAPI()
    app.state.db_pool = pool
    app.state.firebase_app = object()
    app.include_router(staff_api.router)

    with TestClient(
        app,
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        yield SimpleNamespace(
            client=client,
            pool=pool,
            verify=verify,
            writes=writes,
            config=config,
        )


@pytest.mark.parametrize("method,path,body", REQUESTS)
@pytest.mark.parametrize(
    "access",
    ["anonymous", "unapproved", "revoked", "disabled"],
)
def test_kitchen_routes_require_staff(
    kitchen_client, method, path, body, access
):
    d = kitchen_client

    if access == "anonymous":
        d.client.headers.pop("Authorization")
    elif access == "unapproved":
        d.verify.return_value = {
            "uid": "not-staff",
            "staff": False,
        }
    elif access == "revoked":
        d.verify.side_effect = firebase_auth.RevokedIdTokenError(
            "revoked"
        )
    else:
        d.verify.side_effect = firebase_auth.UserDisabledError(
            "disabled"
        )

    response = d.client.request(method, path, json=body)

    assert response.status_code == (
        403 if access == "unapproved" else 401
    )
    d.pool.acquire.assert_not_called()


@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_kitchen_routes_accept_staff(
    kitchen_client, method, path, body
):
    d = kitchen_client
    response = d.client.request(method, path, json=body)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"

    if method == "GET":
        assert response.json()["accepting_orders"] is True
    else:
        assert response.json() == {"ok": True}

    # Availability endpoints must not modify prices.
    if "/items/" in path:
        assert d.writes["update_item"].await_args.kwargs == {
            "price_cents": None,
            "is_available": False,
        }

    if "/options/" in path:
        assert d.writes["update_option"].await_args.kwargs == {
            "price_delta_cents": None,
            "is_available": False,
        }


@pytest.mark.parametrize(
    "route,operation,status",
    [
        ("/staff/kitchen/heartbeat", "heartbeat", 503),
        ("/staff/kitchen/intake", "set_ordering_enabled", 503),
        (
            f"/staff/menu/items/{ITEM_ID}/availability",
            "update_item",
            404,
        ),
        (
            f"/staff/menu/options/{ITEM_ID}/availability",
            "update_option",
            404,
        ),
    ],
)
def test_kitchen_missing_records(
    kitchen_client, route, operation, status
):
    d = kitchen_client
    d.writes[operation].return_value = False

    if operation == "heartbeat":
        response = d.client.post(route)
    else:
        key = (
            "ordering_enabled"
            if operation == "set_ordering_enabled"
            else "is_available"
        )
        response = d.client.patch(route, json={key: False})

    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"


def test_kitchen_rejects_price_edit(kitchen_client):
    d = kitchen_client

    response = d.client.patch(
        f"/staff/menu/items/{ITEM_ID}/availability",
        json={
            "is_available": False,
            "price_cents": 1,
        },
    )

    assert response.status_code == 422
    d.writes["update_item"].assert_not_awaited()


def test_kitchen_read_does_not_send_heartbeat(kitchen_client):
    d = kitchen_client
    d.config.return_value.kitchen_last_seen_at = (
        NOW - timedelta(seconds=30)
    )

    response = d.client.get("/staff/kitchen")

    assert response.json()["block_reason"] == "kitchen_offline"
    assert response.json()["accepting_orders"] is False
    d.writes["heartbeat"].assert_not_awaited()


def test_kitchen_database_failure(kitchen_client):
    d = kitchen_client
    d.writes["heartbeat"].side_effect = TimeoutError(
        "private connection details"
    )

    response = d.client.post("/staff/kitchen/heartbeat")

    assert response.status_code == 503
    assert "private" not in response.text