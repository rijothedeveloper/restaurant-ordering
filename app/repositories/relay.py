import base64
import hashlib
import hmac
from uuid import UUID, uuid4

import asyncpg

from app.schemas.relay import Setup
from app.schemas.voice import TERMINAL_STATUSES
import logging

logger = logging.getLogger("uvicorn.error")


def token_for(secret: bytes, session_id: UUID, call_id: UUID) -> str:
    # Reproduce a pending credential for retries without storing plaintext.
    message = f"telnyx-relay-v1:{session_id}:{call_id}".encode()
    digest = hmac.digest(secret, message, "sha256")
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RelayRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def issue(self, call_id: UUID, secret: bytes) -> str | None:
        async with self.pool.acquire(timeout=3) as connection:
            async with connection.transaction():
                call = await connection.fetchrow(
                    """
                    SELECT *
                    FROM public.voice_calls
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    call_id,
                )

                if (
                    not call
                    or not call["voice_requested"]
                    or call["status"] in TERMINAL_STATUSES
                ):
                    return None

                row = await connection.fetchrow(
                    """
                    SELECT *, expires_at > clock_timestamp() AS fresh
                    FROM public.voice_relay_sessions
                    WHERE call_id = $1
                    """,
                    call_id,
                )

                if row:
                    # Never renew a used, closed, or expired session.
                    if not row["fresh"] or row["consumed_at"] or row["closed_at"]:
                        return None

                    token = token_for(secret, row["id"], call_id)

                    # A changed application secret invalidates the pending token.
                    if hmac.compare_digest(token_hash(token), row["token_hash"]):
                        return token
                    return None

                session_id = uuid4()
                token = token_for(secret, session_id, call_id)

                await connection.execute(
                    """
                    INSERT INTO public.voice_relay_sessions
                        (id, call_id, token_hash, expires_at)
                    VALUES (
                        $1,
                        $2,
                        $3,
                        clock_timestamp() + interval '60 seconds'
                    )
                    """,
                    session_id,
                    call_id,
                    token_hash(token),
                )

                return token

    async def claim(self, setup: Setup) -> UUID | None:
        digest = token_hash(setup.parameters.relay_token)

        async with self.pool.acquire(timeout=3) as connection:
            async with connection.transaction():
                # Use the same call lock as the status webhook.
                call = await connection.fetchrow(
                    """
                    SELECT c.*
                    FROM public.voice_calls c
                    JOIN public.voice_relay_sessions s ON s.call_id = c.id
                    WHERE s.token_hash = $1
                    FOR UPDATE OF c
                    """,
                    digest,
                )

                if (
                    not call
                    or not call["voice_requested"]
                    or call["status"] in TERMINAL_STATUSES
                ):
                    return None

                # Setup metadata must match the call verified by the HTTP webhook.
                account_matches = setup.account_sid == call["account_sid"]
                call_matches = setup.call_control_id == call["call_sid"]
                destination_conflicts = (
                    setup.to_phone is not None and setup.to_phone != call["to_phone"]
                )

                if not account_matches or not call_matches or destination_conflicts:
                    logger.warning(
                        "Relay metadata mismatch account_matches=%s "
                        "call_matches=%s destination_conflicts=%s",
                        account_matches,
                        call_matches,
                        destination_conflicts,
                    )
                    return None

                # Exactly one competing connection can consume this credential.
                return await connection.fetchval(
                    """
                    UPDATE public.voice_relay_sessions
                    SET consumed_at = clock_timestamp(),
                        provider_session_id = $2
                    WHERE token_hash = $1
                      AND consumed_at IS NULL
                      AND closed_at IS NULL
                      AND expires_at > clock_timestamp()
                    RETURNING id
                    """,
                    digest,
                    setup.session_id,
                )

    async def active(self, session_id: UUID) -> bool:
        return bool(
            await self.pool.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM public.voice_relay_sessions s
                    JOIN public.voice_calls c ON c.id = s.call_id
                    WHERE s.id = $1
                      AND s.consumed_at IS NOT NULL
                      AND s.closed_at IS NULL
                      AND c.ended_at IS NULL
                      AND NOT (c.status = ANY($2::text[]))
                )
                """,
                session_id,
                list(TERMINAL_STATUSES),
            )
        )

    async def finish(self, session_id: UUID) -> None:
        await self.pool.execute(
            """
            UPDATE public.voice_relay_sessions
            SET closed_at = COALESCE(closed_at, clock_timestamp())
            WHERE id = $1
            """,
            session_id,
        )
