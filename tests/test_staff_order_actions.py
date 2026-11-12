from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from firebase_admin import auth as firebase_auth
from pydantic import ValidationError

from app.api import staff as staff_api
from app.auth import firebase_auth as auth_provider
from app.schemas.order import OrderResponse
from app.schemas.staff_order import StaffOrderStatusChange
from app.services.staff_orders import StaffOrderError


@pytest.fixture
def action_client(monkeypatch):
    order = OrderResponse.model_validate(
        {
            "order_id": uuid4(),
            "order_number": 1,
            "cart_id": uuid4(),
            "quote_id": uuid4(),
            "cart_version": 1,
            "status": "ACCEPTED",
            "payment_status": "UNPAID",
            "currency": "USD",
            "items": [
                {
                    "line_id": uuid4(),
                    "item_id": uuid4(),
                    "item_name": "Bowl",
                    "quantity": 1,
                    "options": [],
                    "unit_price_cents": 1000,
                    "line_total_cents": 1000,
                }
            ],
            "subtotal_cents": 1000,
            "tax_rate_bps": 1000,
            "tax_cents": 100,
            "total_cents": 1100,
            "confirmed_at": "2026-11-12T18:00:00Z",
            "placed_at": "2026-11-12T18:00:01Z",
            "pickup_at": "2026-11-12T18:20:01Z",
        }
    )

    connection = object()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    # Keep require_staff active; mock only the external Firebase call.
    verify = Mock(return_value={"uid": "approved-staff", "staff": True})
    monkeypatch.setattr(auth_provider, "verify_id_token", verify)

    status_action = AsyncMock(return_value=order)
    payment_action = AsyncMock(
        return_value=order.model_copy(
            update={
                "status": "READY",
                "payment_status": "PAID",
            }
        )
    )

    monkeypatch.setattr(staff_api, "change_order_status", status_action)
    monkeypatch.setattr(staff_api, "mark_order_paid", payment_action)

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
            status_action=status_action,
            payment_action=payment_action,
            order=order,
            connection=connection,
        )


def send_action(d, action):
    path = f"/staff/orders/{d.order.order_id}"

    if action == "status":
        return d.client.patch(
            path + "/status",
            json={
                "expected_status": "PLACED",
                "status": "ACCEPTED",
            },
        )

    return d.client.post(path + "/paid")


@pytest.mark.parametrize("action", ["status", "paid"])
@pytest.mark.parametrize(
    "access",
    ["anonymous", "unapproved", "revoked", "disabled"],
)
def test_staff_actions_require_approved_session(action_client, action, access):
    d = action_client

    if access == "anonymous":
        d.client.headers.pop("Authorization")
    elif access == "unapproved":
        d.verify.return_value = {
            "uid": "someone-else",
            "staff": False,
        }
    elif access == "revoked":
        d.verify.side_effect = firebase_auth.RevokedIdTokenError("revoked")
    else:
        d.verify.side_effect = firebase_auth.UserDisabledError("disabled")

    response = send_action(d, action)

    assert response.status_code == (403 if access == "unapproved" else 401)

    # Unauthorized requests must stop before reaching the database.
    d.pool.acquire.assert_not_called()
    d.status_action.assert_not_awaited()
    d.payment_action.assert_not_awaited()


@pytest.mark.parametrize("action", ["status", "paid"])
def test_staff_action_uses_verified_identity(action_client, action):
    d = action_client
    response = send_action(d, action)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"

    operation = d.status_action if action == "status" else d.payment_action
    assert operation.await_args.kwargs["staff_user_id"] == "approved-staff"
    assert operation.await_args.args == (d.connection,)
    assert d.verify.call_args.kwargs["check_revoked"] is True


@pytest.mark.parametrize("action", ["status", "paid"])
@pytest.mark.parametrize(
    "code,status",
    [("order_not_found", 404), ("stale_order", 409)],
)
def test_staff_action_errors(action_client, action, code, status):
    d = action_client
    operation = d.status_action if action == "status" else d.payment_action
    operation.side_effect = StaffOrderError(code, "Action unavailable")

    response = send_action(d, action)

    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("action", ["status", "paid"])
def test_staff_action_database_failure(action_client, action):
    d = action_client
    operation = d.status_action if action == "status" else d.payment_action
    operation.side_effect = TimeoutError("private connection details")

    response = send_action(d, action)

    assert response.status_code == 503
    assert "private" not in response.text


@pytest.mark.parametrize("reason", [None, "", "   ", "x" * 301])
def test_staff_rejection_needs_reason(reason):
    with pytest.raises(ValidationError):
        StaffOrderStatusChange(
            expected_status="PLACED",
            status="REJECTED",
            reason=reason,
        )


def test_staff_request_rejects_actor_in_body(action_client):
    d = action_client

    response = d.client.patch(
        f"/staff/orders/{d.order.order_id}/status",
        json={
            "expected_status": "PLACED",
            "status": "ACCEPTED",
            "staff_user_id": "forged",
        },
    )

    assert response.status_code == 422
    d.status_action.assert_not_awaited()
