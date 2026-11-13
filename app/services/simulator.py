from __future__ import annotations

import asyncio
import json
import shlex
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from app.schemas.tools import ToolError, ToolResult

if TYPE_CHECKING:
    from app.services.tools import ToolDispatcher

VERSIONED = {"add_cart_item", "update_cart_item", "remove_cart_item", "create_quote"}
CART_RESULTS = {"get_cart", "add_cart_item", "update_cart_item", "remove_cart_item"}


class CommandError(ValueError):
    pass


def parse_command(text: str) -> tuple[str, dict[str, Any]]:
    if len(text) > 4096:
        raise CommandError("Command is too long.")

    try:
        words = shlex.split(text)
    except ValueError:
        raise CommandError("Check the quotation marks in your command.") from None

    if not words:
        raise CommandError("Enter a command; type help to see the available commands.")

    verb, *parts = words
    verb = verb.casefold()

    def identifier(value: str) -> str:
        try:
            return str(UUID(value))
        except ValueError:
            raise CommandError(
                "Copy a complete UUID from the menu, cart, or quote."
            ) from None

    if verb in {"menu", "cart", "quote"} and not parts:
        return {"menu": "get_menu", "cart": "get_cart", "quote": "create_quote"}[
            verb
        ], {}

    if verb in {"search", "staff"} and parts:
        field = "query" if verb == "search" else "reason"
        function = "search_menu" if verb == "search" else "request_staff"
        return function, {field: " ".join(parts)}

    if verb in {"add", "update"} and len(parts) >= 2:
        target, quantity, *options = parts

        if not quantity.isascii() or not quantity.isdecimal() or len(quantity) > 2:
            raise CommandError("Quantity must be a whole number from 1 through 20.")

        # The dispatcher still enforces quantity bounds and modifier validity.
        return (
            "add_cart_item" if verb == "add" else "update_cart_item",
            {
                "item_id" if verb == "add" else "line_id": identifier(target),
                "quantity": int(quantity),
                "option_ids": [identifier(option) for option in options],
            },
        )

    if verb in {"remove", "place"} and len(parts) == 1:
        return (
            "remove_cart_item" if verb == "remove" else "place_order",
            {"line_id" if verb == "remove" else "quote_id": identifier(parts[0])},
        )

    raise CommandError("Unknown command or wrong argument count. Type help.")


def simulator_error(code: str, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolError(code=code, message=message))


class TextSimulator:
    def __init__(self, dispatcher: ToolDispatcher) -> None:
        # This ID is local correlation data, not yet a persisted telephone call.
        self.session_id = uuid4()
        self.stopped = False
        self._dispatcher = dispatcher
        self._version: int | None = None
        self._lock = asyncio.Lock()

    async def execute(self, text: str) -> ToolResult:
        async with self._lock:
            if self.stopped:
                return simulator_error(
                    "staff_requested", "Automated actions have stopped."
                )

            try:
                name, arguments = parse_command(text)
            except CommandError as error:
                return simulator_error("invalid_command", str(error))

            if name in VERSIONED:
                if self._version is None:
                    return simulator_error(
                        "cart_refresh_required",
                        "Run cart and review its current contents first.",
                    )

                # Use the version already observed; never silently fetch a newer one.
                arguments["expected_version"] = self._version

            result = await self._dispatcher.dispatch(name, json.dumps(arguments))

            if result.ok and name in CART_RESULTS:
                self._version = result.data["version"]

            elif not result.ok and (
                name == "get_cart"
                or result.error.code
                in {"cart_version_conflict", "quote_stale", "result_unknown"}
            ):
                # The next edit must follow an explicit cart read.
                self._version = None

            if result.ok and name == "request_staff":
                self.stopped = True

            return result