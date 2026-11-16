from __future__ import annotations

import asyncio
import json
import shlex
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

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


@dataclass(frozen=True)
class PreparedCommand:
    session_id: UUID
    execution_id: UUID

    # Immutable text prevents later dictionary changes from altering a retry.
    payload_json: str


class ExecutionResult(ToolResult):
    execution_id: UUID
    replayed: bool = False


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
        self._offered: UUID | None = None
        self._receipt: tuple[UUID, UUID] | None = None
        self._draft: dict[str, Any] | None = None
        self._pending_quote: UUID | None = None

    def prepare(
        self,
        text: str,
        *,
        execution_id: UUID | None = None,
    ) -> PreparedCommand:
        # The trusted adapter prepares requests; these are not model arguments.
        name, arguments = parse_command(text)

        payload = {
            "name": name,
            "arguments": arguments,
            "version": (self._version if name in VERSIONED | {"options"} else None),
            "draft": (self._draft if name in {"options", "discard"} else None),
            "quote_id": (
                str(self._pending_quote)
                if name == "no" and self._pending_quote
                else None
            ),
            "receipt": (
                [str(value) for value in self._receipt]
                if name == "yes" and self._receipt
                else None
            ),
        }

        # Freeze the observed version, draft, and confirmation context.
        return PreparedCommand(
            session_id=self.session_id,
            execution_id=execution_id or uuid4(),
            payload_json=json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )

    async def execute(self, text: str) -> ToolResult:
        # Convenience entry point: a new command receives a new execution ID.
        async with self._lock:
            try:
                request = self.prepare(text)
            except CommandError as error:
                self._offered = self._receipt = None

                # Revoke any older prepared confirmation in PostgreSQL too.
                return await self._run(
                    {
                        "name": "_invalid",
                        "arguments": {"message": str(error)},
                    }
                )

            return await self._submit(request)

    async def submit(self, request: PreparedCommand) -> ToolResult:
        # Retry by submitting the original prepared request again.
        async with self._lock:
            return await self._submit(request)

    async def _submit(self, request: PreparedCommand) -> ToolResult:
        if request.session_id != self.session_id:
            return failure(
                "context_mismatch",
                "This request belongs to another conversation.",
            )

        command = json.loads(request.payload_json)

        if command["name"] == "yes":
            # The prepared request already captured the receipt.
            self._receipt = None
        elif command["name"] not in {
            "get_menu",
            "search_menu",
            "get_cart",
            "status",
        }:
            self._offered = self._receipt = None

        return await self._run(command, request=request)

    async def replay(self, execution_id: UUID) -> ToolResult:
        # Recovery after restart only reads a saved result.
        # It never reconstructs or executes a missing request.
        async with self._lock:
            return await self._run({}, replay_id=execution_id)

    async def readback_completed(self, quote_id: UUID) -> ToolResult:
        # Only the trusted CLI calls this after printing the full quote.
        async with self._lock:
            if self._offered != quote_id:
                return failure(
                    "review_required",
                    "Run review before confirming.",
                )

            self._offered = self._receipt = None

            result = await self._run(
                {
                    "name": "_readback",
                    "arguments": {"quote_id": quote_id},
                }
            )

            if result.ok:
                # Keep the receipt token out of the public response.
                token = UUID(result.data.pop("readback_token"))
                self._receipt = (quote_id, token)

            return result

    async def _run(
        self,
        command: dict[str, Any],
        *,
        request: PreparedCommand | None = None,
        replay_id: UUID | None = None,
    ) -> ToolResult:
        execution_id = request.execution_id if request else replay_id
        replayed = False
        staff_requested = False

        try:
            async with self._pool.acquire(timeout=5) as connection:
                async with connection.transaction():
                    repository = SimulatorRepository(connection)
                    session = await repository.get_for_update(self.session_id)

                    if session is None:
                        result = failure(
                            "session_not_found",
                            "This conversation no longer exists.",
                        )
                    else:
                        staff_requested = bool(session.staff_reason)

                        saved = (
                            await repository.get_execution(
                                self.session_id,
                                execution_id,
                            )
                            if execution_id
                            else None
                        )

                        if saved is not None:
                            if request and saved.request_json != request.payload_json:
                                result = failure(
                                    "execution_id_conflict",
                                    "That execution ID was used with "
                                    "a different request.",
                                )
                            else:
                                result = saved.result
                                replayed = True

                        elif replay_id is not None:
                            result = failure(
                                "execution_not_found",
                                "No committed result exists for this ID. "
                                "Run status and cart.",
                            )

                        else:
                            try:
                                # A domain exception rolls back to this
                                # savepoint before we record its rejection.
                                async with connection.transaction():
                                    result = await self._execute(
                                        connection,
                                        session,
                                        command,
                                    )
                            except (
                                CartError,
                                SelectionError,
                                QuoteError,
                                OrderError,
                            ) as error:
                                result = failure(
                                    error.code,
                                    str(error),
                                )

                            if request is not None:
                                # The operation and result commit together.
                                await repository.save_execution(
                                    self.session_id,
                                    execution_id,
                                    request.payload_json,
                                    result,
                                )

        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
        ):
            replayed = False
            result = failure(
                "result_unknown",
                (
                    "Retry the same prepared request, or replay its ID "
                    "after reconnecting."
                    if execution_id is not None
                    else "Could not confirm this step. Run status, then review."
                ),
            )

        # Historical responses must not become current cart observations
        # or enable an old quote for confirmation.
        if (
            replayed
            or replay_id is not None
            or (
                result.error
                and result.error.code
                in {
                    "result_unknown",
                    "execution_id_conflict",
                    "selection_changed",
                }
            )
        ):
            self._forget_observations()
        else:
            self._remember(command.get("name", ""), result)

        if (
            staff_requested
            or (result.data or {}).get("status") == "staff_requested"
            or (result.error and result.error.code == "staff_requested")
        ):
            self.stopped = True

        if execution_id is not None:
            return ExecutionResult(
                **result.model_dump(),
                execution_id=execution_id,
                replayed=replayed,
            )

        return result

    def _forget_observations(self) -> None:
        self._version = self._draft = self._pending_quote = None
        self._offered = self._receipt = None

    def _remember(self, name: str, result: ToolResult) -> None:
        data = result.data or {}

        if result.ok and name in EDITS | {"get_cart", "options"}:
            self._version = result.data["version"]
        elif not result.ok and (
            name == "get_cart"
            or result.error.code in {"cart_version_conflict", "quote_stale"}
        ):
            self._version = None

        if "draft" in data:
            self._draft = deepcopy(data["draft"])
        elif result.ok and name in EDITS | {
            "options",
            "discard",
            "request_staff",
            "yes",
        }:
            self._draft = None

        if result.ok and name == "status":
            self._pending_quote = (
                UUID(data["pending_quote_id"]) if data["pending_quote_id"] else None
            )
        elif name in EDITS | {"options"} or (
            result.ok and name in {"no", "yes", "request_staff"}
        ):
            self._pending_quote = None

        if result.ok and data.get("kind") == "quote":
            self._offered = self._pending_quote = UUID(data["quote"]["quote_id"])

    async def _execute(
        self,
        connection: asyncpg.Connection,
        session: Conversation,
        command: dict[str, Any],
    ) -> ToolResult:
        name = command["name"]
        arguments = command["arguments"]
        version = command.get("version")

        repository = SimulatorRepository(connection)
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
                    "pending_quote_id": (str(pending) if pending else None),
                    "order": (order.model_dump(mode="json") if order else None),
                },
            )

        if session.staff_reason:
            return failure(
                "staff_requested",
                "Automated actions stopped for staff.",
            )

        if name == "_invalid":
            await repository.clear_readback(session.cart_id)
            return failure(
                "invalid_command",
                arguments["message"],
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
                await repository.request_staff(
                    session,
                    result.data["reason"],
                )
            return result

        if name == "yes":
            return await self._place(
                connection,
                session,
                command.get("receipt"),
            )

        if name == "no":
            pending = await repository.pending_quote_id(session.cart_id)
            if pending is None or str(pending) != command.get("quote_id"):
                return failure(
                    "quote_changed",
                    "Run status or review before rejecting a quote.",
                )

            await quotes.clear_pending(session.cart_id)
            return ToolResult(
                ok=True,
                data={"status": "quote_rejected"},
            )

        if name == "discard":
            if session.draft != command.get("draft"):
                return failure(
                    "selection_changed",
                    "Run status and review the current draft.",
                )

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
                return failure(
                    "quote_required",
                    "Run cart, then quote.",
                )

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
            # A correction invalidates quotes displayed by other terminals.
            await quotes.clear_pending(session.cart_id)

        if name in VERSIONED | {"options"}:
            if version is None:
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

                if session.draft != command.get("draft"):
                    return failure(
                        "selection_changed",
                        "Run status and cart before completing this draft.",
                    )

                name = session.draft["name"]
                arguments = {
                    **session.draft["arguments"],
                    **arguments,
                }

                if arguments["expected_version"] != version:
                    await repository.set_draft(session.id, None)
                    return failure(
                        "cart_version_conflict",
                        "The cart changed. Read cart and restate the edit.",
                    )
            else:
                # Use the version frozen when the request was prepared.
                arguments = {
                    **arguments,
                    "expected_version": version,
                }

        if name in EDITS:
            await repository.set_draft(session.id, None)

        result = await dispatcher.dispatch(
            name,
            json.dumps(arguments),
            connection=connection,
        )

        if (
            name in {"add_cart_item", "update_cart_item"}
            and not result.ok
            and result.error.code in SELECTION_ERRORS
        ):
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
        receipt: list[str] | None,
    ) -> ToolResult:
        context = {
            "cart_id": session.cart_id,
            "customer_id": session.customer_id,
        }
        orders = OrderRepository(connection)

        # Recover an existing order before checking confirmation again.
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

        if receipt is None:
            return failure(
                "review_required",
                "Run review and read the full quote first.",
            )

        quote_id, token = (UUID(value) for value in receipt)

        # Preserve the established cart -> catalog -> quote lock order.
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
            return failure(order.code, order.message)

        return ToolResult(
            ok=True,
            data=order.model_dump(mode="json"),
        )
