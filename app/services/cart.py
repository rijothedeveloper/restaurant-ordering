from uuid import UUID

import asyncpg

from app.repositories.cart import CartRepository
from app.repositories.menu import MenuRepository
from app.schemas.cart import (
    AddCartItemRequest,
    AddedCartItem,
    CartLine,
    CartLineIssue,
    CartResponse,
    StoredCartLine,
    UpdateCartItemRequest,
)
from app.schemas.menu import MenuResponse
from app.schemas.selection import SelectionRequest
from app.services.menu_selection import SelectionError, validate_selection


class CartError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def _require_cart(
    repository: CartRepository,
    cart_id: UUID,
    customer_id: UUID,
    *,
    editable: bool,
) -> str:
    status = await repository.get_status_for_update(cart_id, customer_id)

    if status is None:
        raise CartError("cart_not_found", "Cart was not found.")

    if editable and status != "BUILDING":
        raise CartError("cart_not_editable", "This cart cannot be edited.")

    return status


async def _advance_version(
    repository: CartRepository, cart_id: UUID, expected_version: int
) -> int:
    if type(expected_version) is not int or not 0 <= expected_version < 2**63:
        raise CartError(
            "invalid_cart_version", "Cart version must be a non-negative integer."
        )

    version = await repository.advance_version(cart_id, expected_version)

    if version is None:
        raise CartError(
            "cart_version_conflict", "Cart changed. Read it again before editing."
        )

    return version


async def _load_menu(connection: asyncpg.Connection) -> MenuResponse:
    menu = await MenuRepository(connection).get()

    if menu is None:
        raise CartError("restaurant_not_configured", "Restaurant is not configured.")

    return menu


def build_cart_response(
    cart_id: UUID,
    status: str,
    lines: list[StoredCartLine],
    menu: MenuResponse,
    *,
    version: int,
) -> CartResponse:
    menu_items = {item.id: item for c in menu.categories for item in c.items}

    menu_options = {
        option.id: option
        for item in menu_items.values()
        for group in item.modifier_groups
        for option in group.options
    }

    items: list[CartLine] = []

    for line in lines:
        try:
            selection = validate_selection(menu, line)
        except SelectionError as error:
            menu_item = menu_items.get(line.item_id)

            items.append(
                CartLine(
                    **line.model_dump(),
                    item_name=menu_item.name if menu_item else None,
                    options=[
                        menu_options[i] for i in line.option_ids if i in menu_options
                    ],
                    issue=CartLineIssue(code=error.code, message=str(error)),
                )
            )
        else:
            items.append(
                CartLine(
                    **line.model_dump(),
                    item_name=selection.item_name,
                    options=selection.options,
                    unit_price_cents=selection.unit_price_cents,
                    line_total_cents=selection.unit_price_cents * line.quantity,
                )
            )

    subtotal = None

    if all(line.issue is None for line in items):
        subtotal = sum(
            line.line_total_cents for line in items if line.line_total_cents is not None
        )

    return CartResponse(
        cart_id=cart_id,
        status=status,
        version=version,
        items=items,
        subtotal_cents=subtotal,
    )


async def _read_cart(
    repository: CartRepository,
    menu: MenuResponse,
    cart_id: UUID,
    status: str,
    version: int,
) -> CartResponse:
    lines = await repository.list_lines(cart_id)

    return build_cart_response(
        cart_id,
        status,
        lines,
        menu,
        version=version,
    )


async def add_cart_item(
    connection: asyncpg.Connection,
    *,
    cart_id: UUID,
    customer_id: UUID,
    expected_version: int,
    item: AddCartItemRequest,
) -> AddedCartItem:
    repository = CartRepository(connection)

    async with connection.transaction():
        await _require_cart(
            repository,
            cart_id,
            customer_id,
            editable=True,
        )

        version = await _advance_version(
            repository,
            cart_id,
            expected_version,
        )

        menu = await _load_menu(connection)
        selection = validate_selection(menu, item)

        line_id = await repository.insert_line(
            cart_id,
            selection.item_id,
            item.quantity,
            [option.id for option in selection.options],
        )

        result = AddedCartItem(
            **selection.model_dump(),
            line_id=line_id,
            version=version,
            quantity=item.quantity,
            line_total_cents=selection.unit_price_cents * item.quantity,
        )

    return result


async def get_cart(
    connection: asyncpg.Connection,
    *,
    cart_id: UUID,
    customer_id: UUID,
) -> CartResponse:
    repository = CartRepository(connection)

    async with connection.transaction():
        status = await _require_cart(
            repository,
            cart_id,
            customer_id,
            editable=False,
        )

        version = await repository.get_version(cart_id)
        menu = await _load_menu(connection)

        result = await _read_cart(
            repository,
            menu,
            cart_id,
            status,
            version,
        )

    return result


async def update_cart_item(
    connection: asyncpg.Connection,
    *,
    cart_id: UUID,
    customer_id: UUID,
    expected_version: int,
    line_id: UUID,
    item: UpdateCartItemRequest,
) -> CartResponse:
    repository = CartRepository(connection)

    async with connection.transaction():
        status = await _require_cart(
            repository,
            cart_id,
            customer_id,
            editable=True,
        )

        version = await _advance_version(
            repository,
            cart_id,
            expected_version,
        )

        item_id = await repository.get_item_id(cart_id, line_id)

        if item_id is None:
            raise CartError("line_not_found", "Cart line was not found.")

        menu = await _load_menu(connection)

        selection = validate_selection(
            menu,
            SelectionRequest(
                item_id=item_id,
                option_ids=item.option_ids,
            ),
        )

        updated = await repository.replace_line(
            cart_id,
            line_id,
            item.quantity,
            [option.id for option in selection.options],
        )

        if not updated:
            raise CartError("line_not_found", "Cart line was not found.")

        result = await _read_cart(
            repository,
            menu,
            cart_id,
            status,
            version,
        )

    return result


async def remove_cart_item(
    connection: asyncpg.Connection,
    *,
    cart_id: UUID,
    customer_id: UUID,
    expected_version: int,
    line_id: UUID,
) -> CartResponse:
    repository = CartRepository(connection)

    async with connection.transaction():
        status = await _require_cart(
            repository,
            cart_id,
            customer_id,
            editable=True,
        )

        version = await _advance_version(
            repository,
            cart_id,
            expected_version,
        )

        if not await repository.delete_line(cart_id, line_id):
            raise CartError("line_not_found", "Cart line was not found.")

        menu = await _load_menu(connection)

        result = await _read_cart(
            repository,
            menu,
            cart_id,
            status,
            version,
        )

    return result
