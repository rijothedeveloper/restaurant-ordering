import hashlib
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import asyncpg

from app.schemas.voice import STATUS_RANK, TERMINAL_STATUSES, TexmlEvent


class VoiceEventConflict(Exception):
    """An existing provider identity was reused with conflicting data."""


@dataclass(frozen=True)
class VoiceCall:
    id: UUID
    status: str


class VoiceRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def record(
        self,
        event: TexmlEvent,
        kind: Literal["voice", "status"],
    ) -> VoiceCall:
        event_key = "voice" if kind == "voice" else f"status:{event.sequence}"

        # Hash normalized fields, excluding irrelevant provider metadata.
        fingerprint = hashlib.sha256(event.model_dump_json().encode()).hexdigest()

        async with self.pool.acquire(timeout=3) as connection:
            async with connection.transaction():
                # A status callback can arrive before the voice request.
                await connection.execute(
                    """
                    INSERT INTO public.voice_calls
                        (account_sid, connection_id, call_sid, from_phone, to_phone)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (account_sid, call_sid) DO NOTHING
                    """,
                    event.account_sid,
                    event.connection_id,
                    event.call_sid,
                    event.from_phone,
                    event.to_phone,
                )

                # Serialize updates for this call, including concurrent retries.
                call = await connection.fetchrow(
                    """
                    SELECT *
                    FROM public.voice_calls
                    WHERE account_sid = $1 AND call_sid = $2
                    FOR UPDATE
                    """,
                    event.account_sid,
                    event.call_sid,
                )

                if (call["connection_id"], call["to_phone"]) != (
                    event.connection_id,
                    event.to_phone,
                ):
                    raise VoiceEventConflict

                previous = await connection.fetchval(
                    """
                    SELECT fingerprint
                    FROM public.voice_call_events
                    WHERE call_id = $1 AND event_key = $2
                    """,
                    call["id"],
                    event_key,
                )

                if previous is not None:
                    if previous != fingerprint:
                        raise VoiceEventConflict

                    # An identical retry returns the current persisted state.
                    return VoiceCall(call["id"], call["status"])

                await connection.execute(
                    """
                    INSERT INTO public.voice_call_events
                        (call_id, event_key, fingerprint, status)
                    VALUES ($1, $2, $3, $4)
                    """,
                    call["id"],
                    event_key,
                    fingerprint,
                    event.status,
                )

                # A late ringing/answered callback cannot reopen an ended call.
                status = call["status"]
                if STATUS_RANK[event.status] > STATUS_RANK[status]:
                    status = event.status

                await connection.execute(
                    """
                    UPDATE public.voice_calls
                    SET status = $2,
                        voice_requested = voice_requested OR $3,
                        from_phone = COALESCE(from_phone, $4),
                        updated_at = now(),
                        ended_at = CASE
                            WHEN $5 THEN COALESCE(ended_at, now())
                            ELSE ended_at
                        END
                    WHERE id = $1
                    """,
                    call["id"],
                    status,
                    kind == "voice",
                    event.from_phone,
                    status in TERMINAL_STATUSES,
                )

                # The transaction commits before the caller receives this result.
                return VoiceCall(call["id"], status)
