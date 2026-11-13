import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ValidationError

from app.repositories.menu import MenuRepository
from app.schemas.cart import AddCartItemRequest, UpdateCartItemRequest
from app.schemas.order import CheckoutNeedsReview
from app.schemas.tools import (
    FUNCTIONS,
    AddCartItemArguments,
    CartVersionArguments,
    EmptyArguments,
    PlaceOrderArguments,
    RemoveCartItemArguments,
    RequestStaffArguments,
    SearchMenuArguments,
    ToolArguments,
    ToolError,
    ToolResult,
    UpdateCartItemArguments,
)
from app.services.cart import (
    CartError,
    add_cart_item,
    get_cart,
    remove_cart_item,
    update_cart_item,
)
from app.services.menu_selection import SelectionError
from app.services.order import OrderError, place_order
from app.services.quote import QuoteError, create_quote

MAX_ARGUMENT_CHARS = 16_384


@dataclass(frozen=True)
class ToolContext:
    # Construct this in trusted application code, never from function arguments.
    customer_id: UUID
    cart_id: UUID


def failure(
    code: str,
    message: str,
    *,
    fields: list[str] | None = None,
    data: dict[str, Any] | None = None,
) -> ToolResult:
    return ToolResult(
        ok=False,
        data=data,
        error=ToolError(code=code, message=message, fields=fields or []),
    )


class ToolDispatcher:
    def __init__(self, pool: asyncpg.Pool, context: ToolContext) -> None:
        self._pool = pool
        self._context = context

        # One dispatcher per conversation; retain it between function calls.
        self._lock = asyncio.Lock()
        self._staff_reason: str | None = None

    async def dispatch(self, name: str, arguments_json: str) -> ToolResult:
        # Serialize this conversation's actions. Database versions still protect
        # against edits arriving through another process or dispatcher.
        async with self._lock:
            definition = FUNCTIONS.get(name)
            if definition is None:
                return failure("unknown_function", "This function is not available.")

            if (
                not isinstance(arguments_json, str)
                or len(arguments_json) > MAX_ARGUMENT_CHARS
            ):
                return failure("invalid_arguments", "Supply a small JSON object.")

            argument_model, _ = definition

            try:
                # JSON validation accepts UUID strings while keeping integers strict.
                arguments = argument_model.model_validate_json(arguments_json)
            except ValidationError as error:
                # Return field locations without echoing raw input or credentials.
                fields = sorted(
                    {
                        ".".join(str(part) for part in issue["loc"]) or "$"
                        for issue in error.errors(
                            include_url=False,
                            include_context=False,
                            include_input=False,
                        )
                    }
                )
                return failure(
                    "invalid_arguments",
                    "Arguments do not match this function's schema.",
                    fields=fields,
                )

            if isinstance(arguments, RequestStaffArguments):
                # A repeated request keeps the first reason and remains stopped.
                if self._staff_reason is None:
                    self._staff_reason = arguments.reason

                return ToolResult(
                    ok=True,
                    data={
                        "status": "staff_requested",
                        "reason": self._staff_reason,
                        "transfer_started": False,
                    },
                )

            if self._staff_reason is not None:
                return failure(
                    "staff_requested", "Automated actions stopped for staff assistance."
                )

            try:
                # Borrow a connection only for this operation, not the conversation.
                async with self._pool.acquire(timeout=5) as connection:
                    value = await self._execute(connection, name, arguments)

            except (CartError, SelectionError, QuoteError, OrderError) as error:
                return failure(error.code, str(error))

            except (
                asyncpg.PostgresError,
                asyncpg.InterfaceError,
                OSError,
                TimeoutError,
            ):
                # A lost response is not proof that a write failed. Do not retry here.
                return failure(
                    "result_unknown",
                    "Could not confirm the result. Read current state before retrying.",
                )

            if isinstance(value, CheckoutNeedsReview):
                return failure(
                    value.code,
                    value.message,
                    data={
                        "quote": (
                            value.quote.model_dump(mode="json")
                            if value.quote is not None
                            else None
                        )
                    },
                )

            # UUIDs and timestamps become JSON-compatible strings.
            data = (
                value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            )
            return ToolResult(ok=True, data=data)

    async def _execute(
        self, connection: asyncpg.Connection, name: str, arguments: ToolArguments
    ) -> BaseModel | dict[str, Any]:
        context = {
            "cart_id": self._context.cart_id,
            "customer_id": self._context.customer_id,
        }

        if name in {"get_menu", "search_menu"}:
            menu = await MenuRepository(connection).get()
            if menu is None:
                raise CartError(
                    "restaurant_not_configured", "Restaurant is not configured."
                )

            if isinstance(arguments, SearchMenuArguments):
                query = arguments.query.casefold()

                return {
                    "currency": menu.currency,
                    "items": [
                        item.model_dump(mode="json")
                        for category in menu.categories
                        for item in category.items
                        if item.is_available
                        and query in f"{item.name} {item.description}".casefold()
                    ],
                }

            return menu

        # Explicit dispatch prevents arbitrary Python-function or SQL execution.
        match (name, arguments):
            case ("get_cart", EmptyArguments()):
                return await get_cart(connection, **context)

            case ("add_cart_item", AddCartItemArguments() as args):
                return await add_cart_item(
                    connection,
                    **context,
                    expected_version=args.expected_version,
                    item=AddCartItemRequest(
                        item_id=args.item_id,
                        quantity=args.quantity,
                        option_ids=args.option_ids,
                    ),
                )

            case ("update_cart_item", UpdateCartItemArguments() as args):
                return await update_cart_item(
                    connection,
                    **context,
                    expected_version=args.expected_version,
                    line_id=args.line_id,
                    item=UpdateCartItemRequest(
                        quantity=args.quantity, option_ids=args.option_ids
                    ),
                )

            case ("remove_cart_item", RemoveCartItemArguments() as args):
                return await remove_cart_item(
                    connection,
                    **context,
                    expected_version=args.expected_version,
                    line_id=args.line_id,
                )

            case ("create_quote", CartVersionArguments() as args):
                return await create_quote(
                    connection, **context, expected_version=args.expected_version
                )

            case ("place_order", PlaceOrderArguments() as args):
                # The existing service checks saved confirmation and kitchen intake.
                return await place_order(connection, **context, quote_id=args.quote_id)

            case _:
                raise RuntimeError("Function registry and dispatcher disagree")