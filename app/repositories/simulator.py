import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from app.schemas.tools import ToolResult


class SimulatorConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Conversation:
    id: UUID
    customer_id: UUID
    cart_id: UUID
    draft: dict[str, Any] | None
    staff_reason: str | None


@dataclass(frozen=True)
class SavedExecution:
    request_json: str
    result: ToolResult


class SimulatorRepository:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    async def require_database(self, expected_database: str) -> None:
        # Inspect the actual connection as well as checking the URL in the CLI.
        database = await self.connection.fetchval("SELECT current_database()")
        if database != expected_database:
            raise SimulatorConfigurationError(
                f"Simulator requires the {expected_database} database."
            )

    async def create_context(
        self, *, expected_database: str = "restaurant_dev"
    ) -> tuple[UUID, UUID]:
        # Retain the Lesson 2 helper and its atomic customer/cart creation.
        await self.require_database(expected_database)

        async with self.connection.transaction():
            customer_id = await self.connection.fetchval(
                "INSERT INTO public.customers DEFAULT VALUES RETURNING id"
            )
            cart_id = await self.connection.fetchval(
                "INSERT INTO public.carts (customer_id) VALUES ($1) RETURNING id",
                customer_id,
            )

        return customer_id, cart_id

    async def create_session(
        self, *, expected_database: str = "restaurant_dev"
    ) -> UUID:
        # A conversation failure must also roll back its customer and cart.
        async with self.connection.transaction():
            _, cart_id = await self.create_context(expected_database=expected_database)
            return await self.connection.fetchval(
                """
                INSERT INTO public.conversations (cart_id)
                VALUES ($1)
                RETURNING id
                """,
                cart_id,
            )

    async def require_session(
        self,
        session_id: UUID,
        *,
        expected_database: str = "restaurant_dev",
    ) -> None:
        await self.require_database(expected_database)

        exists = await self.connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM public.conversations WHERE id=$1)",
            session_id,
        )
        if not exists:
            raise SimulatorConfigurationError("Simulator session was not found.")

    async def get_for_update(self, session_id: UUID) -> Conversation | None:
        # Call inside a transaction: conversation first, then its cart.
        row = await self.connection.fetchrow(
            """
            SELECT c.id, c.cart_id, c.draft, c.staff_reason, cart.customer_id
            FROM public.conversations c
            JOIN public.carts cart ON cart.id = c.cart_id
            WHERE c.id = $1
            FOR UPDATE OF c
            """,
            session_id,
        )
        if row is None:
            return None

        await self.connection.fetchval(
            "SELECT id FROM public.carts WHERE id=$1 FOR UPDATE",
            row["cart_id"],
        )

        return Conversation(
            id=row["id"],
            customer_id=row["customer_id"],
            cart_id=row["cart_id"],
            # asyncpg's default JSONB codec returns a JSON string.
            draft=(json.loads(row["draft"]) if row["draft"] is not None else None),
            staff_reason=row["staff_reason"],
        )

    async def set_draft(
        self,
        session_id: UUID,
        draft: dict[str, Any] | None,
    ) -> None:
        await self.connection.execute(
            "UPDATE public.conversations SET draft=$2::jsonb WHERE id=$1",
            session_id,
            json.dumps(draft) if draft is not None else None,
        )

    async def request_staff(
        self,
        conversation: Conversation,
        reason: str,
    ) -> None:
        await self.connection.execute(
            """
            UPDATE public.conversations
            SET staff_reason=$2, draft=NULL
            WHERE id=$1
            """,
            conversation.id,
            reason,
        )

        # Existing cart services also reject edits after this handoff.
        await self.connection.execute(
            """
            UPDATE public.carts SET status='HUMAN_REQUIRED'
            WHERE id=$1 AND status='BUILDING'
            """,
            conversation.cart_id,
        )

    async def pending_quote_id(self, cart_id: UUID) -> UUID | None:
        return await self.connection.fetchval(
            "SELECT id FROM public.quotes WHERE cart_id=$1 AND is_pending",
            cart_id,
        )

    async def get_execution(
        self,
        session_id: UUID,
        execution_id: UUID,
    ) -> SavedExecution | None:
        row = await self.connection.fetchrow(
            """
            SELECT request_json, result_json
            FROM public.tool_executions
            WHERE conversation_id=$1 AND execution_id=$2
            """,
            session_id,
            execution_id,
        )
        if row is None:
            return None

        return SavedExecution(
            request_json=row["request_json"],
            result=ToolResult.model_validate_json(row["result_json"]),
        )

    async def save_execution(
        self,
        session_id: UUID,
        execution_id: UUID,
        request_json: str,
        result: ToolResult,
    ) -> None:
        # The caller holds the conversation lock and operation's transaction.
        # Insert completed results; no committed "in progress" row is needed.
        await self.connection.execute(
            """
            INSERT INTO public.tool_executions
                (conversation_id, execution_id, request_json, result_json)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            session_id,
            execution_id,
            request_json,
            result.model_dump_json(),
        )

    async def clear_readback(self, cart_id: UUID) -> None:
        # Revoke receipts captured by an earlier prepared confirmation request.
        # The quote remains available for a fresh review.
        await self.connection.execute(
            """
            UPDATE public.quotes
            SET readback_token=NULL,
                readback_completed_at=NULL,
                confirmed_at=NULL
            WHERE cart_id=$1 AND is_pending
            """,
            cart_id,
        )
