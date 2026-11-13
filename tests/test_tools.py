import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import asyncpg
import pytest
from pydantic import BaseModel

from app.schemas.menu import MenuCategory, MenuItem, MenuResponse
from app.schemas.order import CheckoutNeedsReview
from app.schemas.tools import function_schemas
from app.services import tools
from app.services.cart import CartError
from app.services.menu_selection import SelectionError
from app.services.order import OrderError
from app.services.quote import QuoteError
from app.services.tools import ToolContext, ToolDispatcher

ITEM_ID, LINE_ID, OPTION_ID, QUOTE_ID = [uuid4() for _ in range(4)]

ADD = {
    "item_id": str(ITEM_ID),
    "quantity": 2,
    "option_ids": [str(OPTION_ID)],
    "expected_version": 4,
}

VALID = {
    "get_menu": {},
    "search_menu": {"query": "bowl"},
    "get_cart": {},
    "add_cart_item": ADD,
    "update_cart_item": {
        "line_id": str(LINE_ID),
        "quantity": 3,
        "option_ids": [str(OPTION_ID)],
        "expected_version": 4,
    },
    "remove_cart_item": {"line_id": str(LINE_ID), "expected_version": 4},
    "create_quote": {"expected_version": 4},
    "place_order": {"quote_id": str(QUOTE_ID)},
    "request_staff": {"reason": "Please help"},
}


@pytest.fixture
def harness():
    connection = MagicMock(spec=asyncpg.Connection)

    # Simulate borrowing and releasing one database connection.
    lease = MagicMock()
    lease.__aenter__ = AsyncMock(return_value=connection)
    lease.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock(spec=asyncpg.Pool)
    pool.acquire.return_value = lease

    context = ToolContext(customer_id=uuid4(), cart_id=uuid4())

    return SimpleNamespace(
        connection=connection,
        lease=lease,
        pool=pool,
        context=context,
        dispatcher=ToolDispatcher(pool, context),
    )


def test_schema_contracts_exclude_authorization_and_confirmation():
    schemas = {entry["name"]: entry["parameters"] for entry in function_schemas()}
    assert schemas.keys() == VALID.keys()

    forbidden = {
        "customer_id",
        "cart_id",
        "call_id",
        "price_cents",
        "payment_status",
        "confirmed",
        "readback_token",
        "staff_phone",
        "sql",
    }

    for schema in schemas.values():
        assert schema["additionalProperties"] is False
        assert not forbidden.intersection(schema["properties"])
        assert set(schema.get("required", [])) == set(schema["properties"])

    # Schema generation must produce ordinary JSON data.
    json.dumps(function_schemas())


@pytest.mark.parametrize(
    "name", ["confirm_quote", "complete_quote_readback", "execute_sql"]
)
def test_unknown_functions_never_acquire_database(harness, name):
    result = asyncio.run(harness.dispatcher.dispatch(name, "{}"))

    assert not result.ok and result.error.code == "unknown_function"
    harness.pool.acquire.assert_not_called()


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("get_cart", {"customer_id": str(uuid4())}),
        ("get_cart", {"cart_id": str(uuid4())}),
        ("get_cart", {"call_id": str(uuid4())}),
        ("add_cart_item", {**ADD, "price_cents": 1}),
        ("add_cart_item", {**ADD, "quantity": True}),
        ("add_cart_item", {**ADD, "quantity": "2"}),
        ("add_cart_item", {**ADD, "quantity": 2.0}),
        ("add_cart_item", {**ADD, "quantity": 0}),
        ("add_cart_item", {**ADD, "quantity": 21}),
        ("add_cart_item", {**ADD, "item_id": "not-a-uuid"}),
        ("add_cart_item", {**ADD, "option_ids": ["bad"]}),
        ("add_cart_item", {k: v for k, v in ADD.items() if k != "option_ids"}),
        (
            "update_cart_item",
            {"line_id": str(LINE_ID), "quantity": 3, "expected_version": 4},
        ),
        ("create_quote", {"expected_version": True}),
        ("create_quote", {"expected_version": "4"}),
        ("create_quote", {"expected_version": -1}),
        ("create_quote", {"expected_version": 2**63}),
        ("place_order", {"quote_id": str(QUOTE_ID), "confirmed": True}),
        ("request_staff", {"reason": "help", "staff_phone": "+15555550100"}),
        ("request_staff", {"reason": "   "}),
        ("request_staff", {"reason": "x" * 301}),
        ("search_menu", {"query": "   "}),
    ],
)
def test_invalid_arguments_never_acquire_database(harness, name, arguments):
    result = asyncio.run(harness.dispatcher.dispatch(name, json.dumps(arguments)))

    assert not result.ok and result.error.code == "invalid_arguments"
    assert result.error.fields
    harness.pool.acquire.assert_not_called()


@pytest.mark.parametrize("raw", ["{", "[]", "null", "{}" + " " * 16_384, {}])
def test_malformed_or_oversized_arguments(harness, raw):
    result = asyncio.run(harness.dispatcher.dispatch("get_cart", raw))

    assert not result.ok and result.error.code == "invalid_arguments"
    harness.pool.acquire.assert_not_called()


class SavedResult(BaseModel):
    identifier: UUID
    saved_at: datetime


@pytest.mark.parametrize(
    "name",
    [
        "get_cart",
        "add_cart_item",
        "update_cart_item",
        "remove_cart_item",
        "create_quote",
        "place_order",
    ],
)
def test_services_receive_trusted_context_and_typed_arguments(
    harness, monkeypatch, name
):
    saved = SavedResult(identifier=uuid4(), saved_at=datetime(2026, 11, 13, tzinfo=UTC))
    operation = AsyncMock(return_value=saved)
    monkeypatch.setattr(tools, name, operation)

    result = asyncio.run(harness.dispatcher.dispatch(name, json.dumps(VALID[name])))

    assert result.ok and result.data == saved.model_dump(mode="json")
    operation.assert_awaited_once()

    call = operation.await_args
    assert call.args == (harness.connection,)
    assert call.kwargs["customer_id"] == harness.context.customer_id
    assert call.kwargs["cart_id"] == harness.context.cart_id

    if "expected_version" in VALID[name]:
        assert call.kwargs["expected_version"] == 4

    if "line_id" in VALID[name]:
        assert call.kwargs["line_id"] == LINE_ID

    if name in {"add_cart_item", "update_cart_item"}:
        assert call.kwargs["item"].option_ids == [OPTION_ID]
        assert call.kwargs["item"].quantity == VALID[name]["quantity"]

    if name == "add_cart_item":
        assert call.kwargs["item"].item_id == ITEM_ID

    if name == "place_order":
        assert call.kwargs["quote_id"] == QUOTE_ID

    harness.lease.__aexit__.assert_awaited_once()


def test_menu_search_returns_all_available_matches(harness, monkeypatch):
    items = [
        MenuItem(
            id=uuid4(),
            name=name,
            description="Rice",
            price_cents=1000,
            is_available=available,
        )
        for name, available in [
            ("Chicken Bowl", True),
            ("Veg Bowl", True),
            ("Old Bowl", False),
        ]
    ]

    menu = MenuResponse(
        restaurant_name="Test",
        ordering_enabled=False,
        categories=[MenuCategory(id=uuid4(), name="Bowls", items=items)],
    )
    monkeypatch.setattr(tools.MenuRepository, "get", AsyncMock(return_value=menu))

    async def scenario():
        full = await harness.dispatcher.dispatch("get_menu", "{}")
        found = await harness.dispatcher.dispatch("search_menu", '{"query":"  BOWL  "}')
        absent = await harness.dispatcher.dispatch("search_menu", '{"query":"pizza"}')

        assert full.data == menu.model_dump(mode="json")
        assert [item["id"] for item in found.data["items"]] == [
            str(item.id) for item in items[:2]
        ]
        assert absent.data["items"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [
        CartError("cart_version_conflict", "Read the cart again."),
        SelectionError("too_few_options", "Choose a size."),
        QuoteError("quote_not_confirmed", "Confirm this quote first."),
        OrderError("order_quote_mismatch", "Another quote was placed."),
    ],
)
def test_expected_service_errors_are_returned(harness, monkeypatch, error):
    monkeypatch.setattr(tools, "get_cart", AsyncMock(side_effect=error))

    result = asyncio.run(harness.dispatcher.dispatch("get_cart", "{}"))

    assert not result.ok and result.error.code == error.code
    assert result.error.message == str(error)


def test_checkout_review_is_not_order_success(harness, monkeypatch):
    review = CheckoutNeedsReview(
        code="kitchen_offline", message="Kitchen disconnected."
    )
    monkeypatch.setattr(tools, "place_order", AsyncMock(return_value=review))

    result = asyncio.run(
        harness.dispatcher.dispatch("place_order", json.dumps(VALID["place_order"]))
    )

    assert not result.ok and result.error.code == "kitchen_offline"
    assert result.data == {"quote": None}


def test_database_error_does_not_leak_or_retry(harness, monkeypatch):
    operation = AsyncMock(side_effect=OSError("private-database-address"))
    monkeypatch.setattr(tools, "add_cart_item", operation)

    result = asyncio.run(harness.dispatcher.dispatch("add_cart_item", json.dumps(ADD)))

    assert not result.ok and result.error.code == "result_unknown"
    assert "private-database-address" not in result.model_dump_json()
    operation.assert_awaited_once()


def test_staff_request_stops_all_further_database_actions(harness):
    async def scenario():
        first = await harness.dispatcher.dispatch(
            "request_staff", '{"reason":"  Help please  "}'
        )
        assert first.ok and first.data == {
            "status": "staff_requested",
            "reason": "Help please",
            "transfer_started": False,
        }

        repeated = await harness.dispatcher.dispatch(
            "request_staff", '{"reason":"Different"}'
        )
        assert repeated == first

        for name, arguments in VALID.items():
            if name != "request_staff":
                result = await harness.dispatcher.dispatch(name, json.dumps(arguments))
                assert not result.ok and result.error.code == "staff_requested"

        harness.pool.acquire.assert_not_called()

    asyncio.run(scenario())


def test_dispatcher_serializes_operations(harness, monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def read_cart(*args, **kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"version": calls}

        monkeypatch.setattr(tools, "get_cart", read_cart)

        async with asyncio.timeout(2):
            first = asyncio.create_task(harness.dispatcher.dispatch("get_cart", "{}"))
            await started.wait()

            second = asyncio.create_task(harness.dispatcher.dispatch("get_cart", "{}"))
            await asyncio.sleep(0)

            # The second operation must wait for the first.
            assert calls == 1
            release.set()

            results = await asyncio.gather(first, second)
            assert all(result.ok for result in results) and calls == 2

    asyncio.run(scenario())