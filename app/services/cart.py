from uuid import UUID

import asyncpg

from app.repositories.cart import CartRepository
from app.repositories.menu import MenuRepository
from app.schemas.cart import AddCartItemRequest, AddedCartItem
from app.services.menu_selection import validate_selection


class CartError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def add_cart_item(
    connection: asyncpg.Connection,
    *,
    cart_id: UUID,
    customer_id: UUID,
    item: AddCartItemRequest,
) -> AddedCartItem:
    repository = CartRepository(connection)
    async with connection.transaction():
        status = await repository.get_status_for_update(cart_id, customer_id)
        if status is None:
            raise CartError("cart_not_found", "Cart was not found.")
        if status != "BUILDING":
            raise CartError("cart_not_editable", "This cart cannot be edited.")

        menu = await MenuRepository(connection).get()
        if menu is None:
            raise CartError(
                "restaurant_not_configured", "Restaurant is not configured."
            )
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
            quantity=item.quantity,
            line_total_cents=selection.unit_price_cents * item.quantity,
        )
    return result
