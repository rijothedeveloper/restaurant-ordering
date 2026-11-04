import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest
from pydantic import ValidationError

from app.repositories.cart import CartRepository
from app.repositories.menu import MenuRepository
from app.schemas.cart import AddCartItemRequest, StoredCartLine, UpdateCartItemRequest
from app.schemas.menu import (
    MenuCategory,
    MenuItem,
    MenuResponse,
    ModifierGroup,
    ModifierOption,
)
from app.services.cart import (
    CartError,
    add_cart_item,
    build_cart_response,
    get_cart,
    remove_cart_item,
    update_cart_item,
)
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


@pytest.mark.parametrize(
    "payload",
    [
        {"quantity": 0, "option_ids": []},
        {"quantity": 21, "option_ids": []},
        {"quantity": True, "option_ids": []},
        {"quantity": "2", "option_ids": []},
        {"quantity": 2.0, "option_ids": []},
        {"quantity": None, "option_ids": []},
        {"quantity": 1},
        {"option_ids": []},
        {"quantity": 1, "option_ids": None},
        {"quantity": 1, "option_ids": [], "item_id": str(uuid4())},
    ],
)
def test_cart_update_requires_complete_valid_input(payload: dict) -> None:
    with pytest.raises(ValidationError):
        UpdateCartItemRequest.model_validate(payload)


@pytest.mark.parametrize("quantity", [1, 20])
def test_cart_update_accepts_empty_selection_and_quantity_bounds(quantity: int) -> None:
    item = UpdateCartItemRequest(quantity=quantity, option_ids=[])
    assert item.quantity == quantity
    assert item.option_ids == []


@pytest.mark.parametrize("quantities,expected", [([], 0), ([2], 3398), ([2, 3], 8495)])
def test_cart_subtotal_uses_each_line_once(
    cart_dependencies: SimpleNamespace, quantities: list[int], expected: int
) -> None:
    d = cart_dependencies
    lines = [
        StoredCartLine(
            line_id=uuid4(), item_id=d.item_id, quantity=q, option_ids=[d.option_id]
        )
        for q in quantities
    ]

    response = build_cart_response(
        d.cart_id, "BUILDING", lines, d.get_menu.return_value
    )

    assert response.subtotal_cents == expected
    assert [line.line_id for line in response.items] == [line.line_id for line in lines]
    assert all(line.issue is None for line in response.items)


@pytest.mark.parametrize(
    "case,code",
    [
        ("item", "item_unavailable"),
        ("option", "option_unavailable"),
        ("required", "too_few_options"),
        ("missing", "item_not_found"),
    ],
)
def test_cart_read_preserves_invalid_lines(
    cart_dependencies: SimpleNamespace, case: str, code: str
) -> None:
    d = cart_dependencies
    menu = d.get_menu.return_value
    item = menu.categories[0].items[0]

    line = StoredCartLine(
        line_id=uuid4(), item_id=d.item_id, quantity=2, option_ids=[d.option_id]
    )

    if case == "item":
        item.is_available = False
    elif case == "option":
        item.modifier_groups[0].options[0].is_available = False
    elif case == "required":
        line.option_ids = []
    else:
        line.item_id = uuid4()

    response = build_cart_response(d.cart_id, "BUILDING", [line], menu)

    assert response.subtotal_cents is None
    assert response.items[0].line_id == line.line_id
    assert response.items[0].option_ids == line.option_ids
    assert response.items[0].line_total_cents is None
    assert response.items[0].issue.code == code


def test_cart_zero_price_is_a_valid_subtotal(
    cart_dependencies: SimpleNamespace,
) -> None:
    d = cart_dependencies
    menu = d.get_menu.return_value
    item = menu.categories[0].items[0]

    item.price_cents = 0
    item.modifier_groups[0].options[0].price_delta_cents = 0

    line = StoredCartLine(
        line_id=uuid4(), item_id=d.item_id, quantity=2, option_ids=[d.option_id]
    )

    response = build_cart_response(d.cart_id, "BUILDING", [line], menu)

    assert response.subtotal_cents == 0
    assert response.items[0].issue is None


@pytest.fixture
def cart_edit_dependencies(
    cart_dependencies: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    d = cart_dependencies
    d.line_id = uuid4()
    d.lines = AsyncMock(return_value=[])
    d.get_item_id = AsyncMock(return_value=d.item_id)
    d.replace = AsyncMock(return_value=True)
    d.delete = AsyncMock(return_value=True)

    monkeypatch.setattr(CartRepository, "list_lines", d.lines)
    monkeypatch.setattr(CartRepository, "get_item_id", d.get_item_id)
    monkeypatch.setattr(CartRepository, "replace_line", d.replace)
    monkeypatch.setattr(CartRepository, "delete_line", d.delete)

    return d


def run_cart_operation(d: SimpleNamespace, operation: str):
    context = {"cart_id": d.cart_id, "customer_id": d.customer_id}

    if operation == "read":
        return asyncio.run(get_cart(d.connection, **context))

    if operation == "remove":
        return asyncio.run(remove_cart_item(d.connection, **context, line_id=d.line_id))

    return asyncio.run(
        update_cart_item(
            d.connection,
            **context,
            line_id=d.line_id,
            item=UpdateCartItemRequest(quantity=3, option_ids=[d.option_id]),
        )
    )


@pytest.mark.parametrize("operation", ["read", "update", "remove"])
def test_cart_operations_require_matching_context(
    cart_edit_dependencies: SimpleNamespace, operation: str
) -> None:
    d = cart_edit_dependencies
    d.status.return_value = None

    with pytest.raises(CartError) as error:
        run_cart_operation(d, operation)

    assert error.value.code == "cart_not_found"
    d.get_menu.assert_not_awaited()
    d.replace.assert_not_awaited()
    d.delete.assert_not_awaited()


@pytest.mark.parametrize("operation", ["update", "remove"])
def test_cart_edits_require_building_status(
    cart_edit_dependencies: SimpleNamespace, operation: str
) -> None:
    d = cart_edit_dependencies
    d.status.return_value = "CHECKED_OUT"

    with pytest.raises(CartError) as error:
        run_cart_operation(d, operation)

    assert error.value.code == "cart_not_editable"
    d.replace.assert_not_awaited()
    d.delete.assert_not_awaited()


def test_cart_read_allows_non_building_status(
    cart_edit_dependencies: SimpleNamespace,
) -> None:
    d = cart_edit_dependencies
    d.status.return_value = "CHECKED_OUT"

    response = run_cart_operation(d, "read")

    assert response.status == "CHECKED_OUT"
    assert response.subtotal_cents == 0
    d.replace.assert_not_awaited()
    d.delete.assert_not_awaited()


@pytest.mark.parametrize("operation", ["update", "remove"])
def test_cart_edits_reject_missing_line(
    cart_edit_dependencies: SimpleNamespace, operation: str
) -> None:
    d = cart_edit_dependencies
    d.get_item_id.return_value = None
    d.delete.return_value = False

    with pytest.raises(CartError) as error:
        run_cart_operation(d, operation)

    assert error.value.code == "line_not_found"
    d.get_menu.assert_not_awaited()


def test_cart_update_rejects_invalid_selection_before_write(
    cart_edit_dependencies: SimpleNamespace,
) -> None:
    d = cart_edit_dependencies

    with pytest.raises(SelectionError) as error:
        asyncio.run(
            update_cart_item(
                d.connection,
                cart_id=d.cart_id,
                customer_id=d.customer_id,
                line_id=d.line_id,
                item=UpdateCartItemRequest(quantity=3, option_ids=[]),
            )
        )

    assert error.value.code == "too_few_options"
    d.replace.assert_not_awaited()


def test_cart_update_returns_recalculated_cart(
    cart_edit_dependencies: SimpleNamespace,
) -> None:
    d = cart_edit_dependencies
    d.lines.return_value = [
        StoredCartLine(
            line_id=d.line_id, item_id=d.item_id, quantity=3, option_ids=[d.option_id]
        )
    ]

    response = run_cart_operation(d, "update")

    assert response.subtotal_cents == 5097
    d.replace.assert_awaited_once_with(d.cart_id, d.line_id, 3, [d.option_id])


def test_cart_remove_allows_unavailable_selection(
    cart_edit_dependencies: SimpleNamespace,
) -> None:
    d = cart_edit_dependencies
    d.get_menu.return_value.categories[0].items[0].is_available = False

    response = run_cart_operation(d, "remove")

    assert response.items == []
    assert response.subtotal_cents == 0
    d.delete.assert_awaited_once_with(d.cart_id, d.line_id)
