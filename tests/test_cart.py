import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest
from pydantic import ValidationError

from app.repositories.cart import CartRepository
from app.repositories.menu import MenuRepository
from app.schemas.cart import AddCartItemRequest
from app.schemas.menu import (
    MenuCategory,
    MenuItem,
    MenuResponse,
    ModifierGroup,
    ModifierOption,
)
from app.services.cart import CartError, add_cart_item
from app.services.menu_selection import SelectionError


@pytest.mark.parametrize("quantity", [-1, 0, 21, 1.5, 2.0, True, "2", None])
def test_cart_quantity_rejects_invalid_values(quantity: object) -> None:
    with pytest.raises(ValidationError):
        AddCartItemRequest(item_id=uuid4(), quantity=quantity)


@pytest.mark.parametrize("quantity", [1, 20])
def test_cart_quantity_accepts_boundaries(quantity: int) -> None:
    assert AddCartItemRequest(item_id=uuid4(), quantity=quantity).quantity == quantity


def test_cart_quantity_defaults_to_one() -> None:
    item = AddCartItemRequest(item_id=str(uuid4()))
    assert item.quantity == 1
    assert item.option_ids == []


@pytest.mark.parametrize("field", ["cart_id", "customer_id", "unit_price_cents"])
def test_cart_request_rejects_context_and_prices(field: str) -> None:
    with pytest.raises(ValidationError) as error:
        AddCartItemRequest.model_validate({"item_id": str(uuid4()), field: 0})
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.fixture
def cart_dependencies(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    connection = MagicMock(spec=asyncpg.Connection)
    transaction = connection.transaction.return_value
    transaction.__aexit__.return_value = False
    item_id, option_id = uuid4(), uuid4()
    menu = MenuResponse(
        restaurant_name="Test Kitchen",
        ordering_enabled=False,
        categories=[
            MenuCategory(
                id=uuid4(),
                name="Mains",
                items=[
                    MenuItem(
                        id=item_id,
                        name="Bowl",
                        description="",
                        price_cents=1399,
                        is_available=True,
                        modifier_groups=[
                            ModifierGroup(
                                id=uuid4(),
                                name="Size",
                                min_selections=1,
                                max_selections=1,
                                options=[
                                    ModifierOption(
                                        id=option_id,
                                        name="Large",
                                        price_delta_cents=300,
                                        is_available=True,
                                    )
                                ],
                            )
                        ],
                    )
                ],
            )
        ],
    )
    status = AsyncMock(return_value="BUILDING")
    get_menu = AsyncMock(return_value=menu)
    insert = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(CartRepository, "get_status_for_update", status)
    monkeypatch.setattr(MenuRepository, "get", get_menu)
    monkeypatch.setattr(CartRepository, "insert_line", insert)
    return SimpleNamespace(
        connection=connection,
        transaction=transaction,
        cart_id=uuid4(),
        customer_id=uuid4(),
        item_id=item_id,
        option_id=option_id,
        status=status,
        get_menu=get_menu,
        insert=insert,
    )


def run_add(deps: SimpleNamespace, item: AddCartItemRequest):
    return asyncio.run(
        add_cart_item(
            deps.connection,
            cart_id=deps.cart_id,
            customer_id=deps.customer_id,
            item=item,
        )
    )


@pytest.mark.parametrize("quantity", [1, 2, 20])
def test_cart_add_calculates_line_total(
    cart_dependencies: SimpleNamespace, quantity: int
) -> None:
    d = cart_dependencies
    result = run_add(
        d,
        AddCartItemRequest(
            item_id=d.item_id, option_ids=[d.option_id], quantity=quantity
        ),
    )
    assert result.unit_price_cents == 1699
    assert result.line_total_cents == 1699 * quantity
    assert result.quantity == quantity
    assert result.currency == "USD"
    assert result.line_id == d.insert.return_value
    d.status.assert_awaited_once_with(d.cart_id, d.customer_id)
    d.insert.assert_awaited_once_with(d.cart_id, d.item_id, quantity, [d.option_id])
    d.transaction.__aexit__.assert_awaited_once_with(None, None, None)


@pytest.mark.parametrize(
    "status",
    [None, "CHECKED_OUT", "ABANDONED", "HUMAN_REQUIRED", "AWAITING_CONFIRMATION"],
)
def test_cart_add_rejects_missing_or_locked_cart(
    cart_dependencies: SimpleNamespace, status: str | None
) -> None:
    d = cart_dependencies
    d.status.return_value = status
    with pytest.raises(CartError) as error:
        run_add(d, AddCartItemRequest(item_id=d.item_id, option_ids=[d.option_id]))
    expected = "cart_not_found" if status is None else "cart_not_editable"
    assert error.value.code == expected
    d.get_menu.assert_not_awaited()
    d.insert.assert_not_awaited()


def test_cart_add_requires_restaurant_configuration(
    cart_dependencies: SimpleNamespace,
) -> None:
    d = cart_dependencies
    d.get_menu.return_value = None
    with pytest.raises(CartError) as error:
        run_add(d, AddCartItemRequest(item_id=d.item_id, option_ids=[d.option_id]))
    assert error.value.code == "restaurant_not_configured"
    d.insert.assert_not_awaited()


def test_cart_add_reuses_selection_validation(
    cart_dependencies: SimpleNamespace,
) -> None:
    d = cart_dependencies
    with pytest.raises(SelectionError) as error:
        run_add(d, AddCartItemRequest(item_id=d.item_id))
    assert error.value.code == "too_few_options"
    d.insert.assert_not_awaited()


def test_cart_add_propagates_database_failure(
    cart_dependencies: SimpleNamespace,
) -> None:
    d = cart_dependencies
    d.insert.side_effect = OSError("simulated write failure")
    with pytest.raises(OSError):
        run_add(d, AddCartItemRequest(item_id=d.item_id, option_ids=[d.option_id]))
    assert d.transaction.__aexit__.await_args.args[0] is OSError
