from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any
from uuid import UUID

import asyncpg

from app.repositories.order import OrderRepository
from app.repositories.quote import QuoteRepository
from app.repositories.simulator import Conversation, SimulatorRepository
from app.schemas.order import CheckoutNeedsReview
from app.schemas.tools import ToolError, ToolResult
from app.services.cart import CartError
from app.services.menu_selection import SelectionError
from app.services.order import OrderError, place_order
from app.services.quote import (
    QuoteError,
    complete_quote_readback,
    confirm_quote,
    get_quote,
)
from app.services.tools import ToolContext, ToolDispatcher

EDITS = {
    "add_cart_item",
    "update_cart_item",
    "remove_cart_item",
}
VERSIONED = EDITS | {"create_quote"}

SELECTION_ERRORS = {
    "too_few_options",
    "too_many_options",
    "invalid_option",
    "option_unavailable",
    "duplicate_option",
}


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
        raise CommandError("Enter a command; type help for the list.")

    verb, *parts = words
    verb = verb.casefold()

    def identifier(value: str) -> str:
        try:
            return str(UUID(value))
        except ValueError:
            raise CommandError("Copy a complete UUID from the menu or cart.") from None

    simple = {
        "menu": "get_menu",
        "cart": "get_cart",
        "quote": "create_quote",
    }
    if verb in simple and not parts:
        return simple[verb], {}

    if verb in {"status", "review", "yes", "no", "discard"} and not parts:
        return verb, {}

    if verb in {"search", "staff"} and parts:
        return (
            "search_menu" if verb == "search" else "request_staff",
            {"query" if verb == "search" else "reason": " ".join(parts)},
        )

    if verb in {"add", "update"} and len(parts) >= 2:
        target, quantity, *options = parts

        if not quantity.isascii() or not quantity.isdecimal() or len(quantity) > 2:
            raise CommandError("Quantity must be a whole number from 1 through 20.")

        # The existing dispatcher validates quantity bounds and argument types.
        return (
            "add_cart_item" if verb == "add" else "update_cart_item",
            {
                "item_id" if verb == "add" else "line_id": identifier(target),
                "quantity": int(quantity),
                "option_ids": [identifier(option) for option in options],
            },
        )

    if verb == "remove" and len(parts) == 1:
        return "remove_cart_item", {"line_id": identifier(parts[0])}

    if verb == "options":
        return "options", {"option_ids": [identifier(option) for option in parts]}

    # Neither "place UUID" nor "yes but change ..." is accepted.
    raise CommandError("Unknown command or wrong arguments. Type help.")


def failure(code: str, message: str, **data: Any) -> ToolResult:
    return ToolResult(
        ok=False,
        error=ToolError(code=code, message=message),
        data=data or None,
    )


class TextSimulator:
    def __init__(self, pool: asyncpg.Pool, session_id: UUID) -> None:
        self.session_id = session_id
        self.stopped = False
        self._pool = pool
        self._lock = asyncio.Lock()
        self._version: int | None = None

        # Resume deliberately requires a fresh readback in this terminal.
        self._offered: UUID | None = None
        self._receipt: tuple[UUID, UUID] | None = None

    async def execute(self, text: str) -> ToolResult:
        async with self._lock:
            try:
                name, arguments = parse_command(text)
            except CommandError as error:
                self._offered = self._receipt = None
                return failure("invalid_command", str(error))

            if name not in {
                "get_menu",
                "search_menu",
                "get_cart",
                "status",
                "yes",
            }:
                # Corrections, rejection, and fresh quotes disarm the old offer.
                self._offered = self._receipt = None

            return await self._run(name, arguments)

    async def readback_completed(self, quote_id: UUID) -> ToolResult:
        # Only the trusted CLI calls this after printing the complete quote.
        async with self._lock:
            if self._offered != quote_id:
                return failure(
                    "review_required",
                    "Run review before confirming.",
                )

            self._offered = self._receipt = None
            result = await self._run("_readback", {"quote_id": quote_id})

            if result.ok:
                # Keep the token out of the public tool/console response.
                token = UUID(result.data.pop("readback_token"))
                self._receipt = (quote_id, token)

            return result

    async def _run(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        try:
            async with self._pool.acquire(timeout=5) as connection:
                async with connection.transaction():
                    result = await self._execute(connection, name, arguments)

            # Local state changes happen after the database commit.
        except (CartError, SelectionError, QuoteError, OrderError) as error:
            result = failure(error.code, str(error))
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
        ):
            self._offered = self._receipt = None
            result = failure(
                "result_unknown",
                "Run status, then cart. Do not blindly repeat an edit.",
            )

        if result.ok and name in EDITS | {"get_cart", "options"}:
            self._version = result.data["version"]
        elif not result.ok and (
            name == "get_cart"
            or result.error.code
            in {
                "cart_version_conflict",
                "quote_stale",
                "result_unknown",
            }
        ):
            self._version = None

        if result.ok and result.data.get("kind") == "quote":
            self._offered = UUID(result.data["quote"]["quote_id"])

        if (result.data or {}).get("status") == "staff_requested" or (
            result.error is not None and result.error.code == "staff_requested"
        ):
            self.stopped = True

        return result

    async def _execute(
        self,
        connection: asyncpg.Connection,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        repository = SimulatorRepository(connection)
        session = await repository.get_for_update(self.session_id)

        if session is None:
            return failure(
                "session_not_found",
                "This simulator session no longer exists.",
            )

        # Customer/cart identity always comes from persisted server state.
        context = {
            "customer_id": session.customer_id,
            "cart_id": session.cart_id,
        }
        quotes = QuoteRepository(connection)
        orders = OrderRepository(connection)

        if name == "status":
            order = await orders.get_for_cart(**context)
            pending = await repository.pending_quote_id(session.cart_id)

            state = "building"
            if session.staff_reason:
                state = "staff_requested"
            elif order:
                state = "order_placed"
            elif session.draft:
                state = "selection_pending"
            elif pending:
                state = "quote_pending"

            return ToolResult(
                ok=True,
                data={
                    "status": state,
                    "session_id": str(session.id),
                    "cart_id": str(session.cart_id),
                    "draft": session.draft,
                    "staff_reason": session.staff_reason,
                    "pending_quote_id": str(pending) if pending else None,
                    "order": (order.model_dump(mode="json") if order else None),
                },
            )

        if session.staff_reason:
            return failure(
                "staff_requested",
                "Automated actions stopped for staff.",
            )

        dispatcher = ToolDispatcher(
            self._pool,
            ToolContext(**context),
        )

        if name == "request_staff":
            result = await dispatcher.dispatch(
                name,
                json.dumps(arguments),
                connection=connection,
            )
            if result.ok:
                await quotes.clear_pending(session.cart_id)
                await repository.request_staff(session, result.data["reason"])
            return result

        if name == "yes":
            return await self._place(connection, session)

        if name == "no":
            await quotes.clear_pending(session.cart_id)
            return ToolResult(
                ok=True,
                data={"status": "quote_rejected"},
            )

        if name == "discard":
            await quotes.clear_pending(session.cart_id)
            await repository.set_draft(session.id, None)
            return ToolResult(
                ok=True,
                data={"status": "selection_discarded"},
            )

        if session.draft and name in {
            "create_quote",
            "review",
            "_readback",
        }:
            return failure(
                "selection_pending",
                "Finish with options, or discard the draft.",
                draft=session.draft,
            )

        if name == "review":
            quote_id = await repository.pending_quote_id(session.cart_id)
            if quote_id is None:
                return failure("quote_required", "Run cart, then quote.")

            quote = await get_quote(
                connection,
                quote_id=quote_id,
                **context,
            )
            return ToolResult(
                ok=True,
                data={
                    "kind": "quote",
                    "quote": quote.model_dump(mode="json"),
                },
            )

        if name == "_readback":
            receipt = await complete_quote_readback(
                connection,
                **arguments,
                **context,
            )
            return ToolResult(
                ok=True,
                data={
                    "status": "awaiting_confirmation",
                    "readback_token": str(receipt.readback_token),
                },
            )

        if name in EDITS | {"options"}:
            # A correction also disarms quotes shown by another terminal.
            await quotes.clear_pending(session.cart_id)

        if name in VERSIONED | {"options"}:
            if self._version is None:
                return failure(
                    "cart_refresh_required",
                    "Run cart and review its contents first.",
                )

            if name == "options":
                if session.draft is None:
                    return failure(
                        "no_pending_selection",
                        "Start with add or update.",
                    )

                name = session.draft["name"]
                arguments = {
                    **session.draft["arguments"],
                    **arguments,
                }

                # Never silently apply an old intention to a newer cart.
                if arguments["expected_version"] != self._version:
                    await repository.set_draft(session.id, None)
                    return failure(
                        "cart_version_conflict",
                        "The cart changed. Read cart and restate the edit.",
                    )
            else:
                arguments = {
                    **arguments,
                    "expected_version": self._version,
                }

        if name in EDITS:
            await repository.set_draft(session.id, None)

        result = await dispatcher.dispatch(
            name,
            json.dumps(arguments),
            connection=connection,
        )

        if name in {"add_cart_item", "update_cart_item"} and not result.ok:
            if result.error.code in SELECTION_ERRORS:
                draft = {
                    "name": name,
                    "arguments": arguments,
                }
                await repository.set_draft(session.id, draft)

                result = result.model_copy(
                    update={
                        "data": {
                            "draft": draft,
                            "next": (
                                "Read menu; use options with the complete "
                                "option list, or discard."
                            ),
                        }
                    }
                )

        if name == "create_quote" and result.ok:
            result = ToolResult(
                ok=True,
                data={"kind": "quote", "quote": result.data},
            )

        return result

    async def _place(
        self,
        connection: asyncpg.Connection,
        session: Conversation,
    ) -> ToolResult:
        context = {
            "cart_id": session.cart_id,
            "customer_id": session.customer_id,
        }
        orders = OrderRepository(connection)

        # Recover an already committed order before requiring confirmation again.
        existing = await orders.get_for_cart(**context)
        if existing is not None:
            return ToolResult(
                ok=True,
                data=existing.model_dump(mode="json"),
            )

        if session.draft:
            return failure(
                "selection_pending",
                "Finish or discard the pending selection.",
            )

        if self._receipt is None:
            return failure(
                "review_required",
                "Run review and read the full quote first.",
            )

        quote_id, token = self._receipt
        self._receipt = None

        # Match the ordering service's lock order: cart -> catalog -> quote.
        await orders.lock_catalog()

        await confirm_quote(
            connection,
            **context,
            quote_id=quote_id,
            readback_token=token,
            answer="yes",
        )

        order = await place_order(
            connection,
            **context,
            quote_id=quote_id,
        )

        if isinstance(order, CheckoutNeedsReview):
            # Commit its invalidation/replacement and require another readback.
            return failure(order.code, order.message)

        return ToolResult(
            ok=True,
            data=order.model_dump(mode="json"),
        )
