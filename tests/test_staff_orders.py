from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth

from app.config import Settings
from app.main import create_app


@pytest.fixture
def sample_order():
    now = datetime.now(UTC)

    return {
        "order_id": uuid4(),
        "order_number": 42,
        "cart_id": uuid4(),
        "quote_id": uuid4(),
        "cart_version": 1,
        "status": "PLACED",
        "payment_status": "UNPAID",
        "currency": "USD",
        "subtotal_cents": 1699,
        "tax_rate_bps": 1000,
        "tax_cents": 170,
        "total_cents": 1869,
        "confirmed_at": now - timedelta(seconds=1),
        "placed_at": now,
        "pickup_at": now + timedelta(minutes=20),
    }


@pytest.fixture
def staff_client(monkeypatch, sample_order):
    connection = SimpleNamespace(
        fetch=AsyncMock(return_value=[sample_order]),
        fetchrow=AsyncMock(return_value=sample_order),
        fetchval=AsyncMock(return_value=uuid4()),
    )

    acquire = MagicMock()
    acquire.return_value.__aenter__.return_value = connection
    pool = SimpleNamespace(acquire=acquire)

    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgres://unused:unused@localhost/restaurant_test",
        firebase_project_id=None,
    )

    app = create_app(settings)
    app.state.firebase_app = object()
    app.state.db_pool = pool

    verifier = Mock(return_value={"uid": "staff-user", "staff": True})
    monkeypatch.setattr(auth, "verify_id_token", verifier)

    # Skip lifespan: these tests replace database and Firebase network I/O.
    client = TestClient(
        app,
        headers={"Authorization": "Bearer test-token"},
    )

    try:
        yield SimpleNamespace(
            client=client,
            connection=connection,
            pool=pool,
            verifier=verifier,
        )
    finally:
        client.close()


def test_active_queue(staff_client, sample_order):
    response = staff_client.client.get("/staff/orders")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()[0]["order_id"] == str(sample_order["order_id"])
    assert response.json()[0]["total_cents"] == 1869
    assert "cart_id" not in response.json()[0]

    staff_client.verifier.assert_called_once_with(
        "test-token",
        app=staff_client.client.app.state.firebase_app,
        check_revoked=True,
    )


def test_empty_queue(staff_client):
    staff_client.connection.fetch.return_value = []

    response = staff_client.client.get("/staff/orders")

    assert response.status_code == 200
    assert response.json() == []


def test_order_detail_uses_snapshots(staff_client, sample_order):
    staff_client.connection.fetch.return_value = [
        {
            "line_id": uuid4(),
            "item_id": uuid4(),
            "item_name": "Original bowl",
            "quantity": 1,
            "unit_price_cents": 1699,
            "line_total_cents": 1699,
            "options": "[]",
        }
    ]

    response = staff_client.client.get(f"/staff/orders/{sample_order['order_id']}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"][0]["item_name"] == "Original bowl"
    assert response.json()["payment_status"] == "UNPAID"


def test_unknown_order(staff_client):
    staff_client.connection.fetchval.return_value = None

    response = staff_client.client.get(f"/staff/orders/{uuid4()}")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    staff_client.connection.fetchrow.assert_not_awaited()


def test_invalid_order_id(staff_client):
    response = staff_client.client.get("/staff/orders/not-a-uuid")

    assert response.status_code == 422
    staff_client.connection.fetchval.assert_not_awaited()


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize(
    "case,status",
    [
        ("anonymous", 401),
        ("unapproved", 403),
        ("revoked", 401),
    ],
)
def test_staff_guard_runs_before_database(staff_client, detail, case, status):
    path = f"/staff/orders/{uuid4()}" if detail else "/staff/orders"

    if case == "anonymous":
        staff_client.client.headers.pop("Authorization")
    elif case == "unapproved":
        staff_client.verifier.return_value = {"uid": "unapproved-user"}
    else:
        staff_client.verifier.side_effect = auth.RevokedIdTokenError("Revoked")

    response = staff_client.client.get(path)

    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    staff_client.pool.acquire.assert_not_called()


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("private timeout"),
        asyncpg.PostgresError("private database detail"),
    ],
)
def test_database_failure_is_503(staff_client, detail, error):
    path = f"/staff/orders/{uuid4()}" if detail else "/staff/orders"

    method = (
        staff_client.connection.fetchval if detail else staff_client.connection.fetch
    )
    method.side_effect = error

    response = staff_client.client.get(path)

    assert response.status_code == 503
    assert response.json() == {"detail": "Kitchen orders unavailable"}
    assert response.headers["cache-control"] == "no-store"


def test_missing_pool_is_503(staff_client):
    staff_client.client.app.state.db_pool = None

    response = staff_client.client.get("/staff/orders")

    assert response.status_code == 503


def test_connection_release_failure_is_503(staff_client):
    staff_client.pool.acquire.return_value.__aexit__.side_effect = OSError(
        "private detail"
    )

    response = staff_client.client.get("/staff/orders")

    assert response.status_code == 503
    assert response.json() == {"detail": "Kitchen orders unavailable"}
