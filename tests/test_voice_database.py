import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest

from app.repositories.voice import VoiceEventConflict, VoiceRepository
from app.schemas.voice import TexmlEvent


@asynccontextmanager
async def database():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL tests")

    if urlsplit(dsn).path != "/restaurant_test":
        pytest.fail("Voice database tests require the restaurant_test database")

    pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=4,
        command_timeout=5,
    )
    account = f"test-{uuid4()}"

    try:
        assert await pool.fetchval("SELECT current_database()") == "restaurant_test"
        yield pool, account
    finally:
        # Clean up only this test's records.
        await pool.execute(
            "DELETE FROM public.voice_calls WHERE account_sid = $1",
            account,
        )
        await pool.close()


def event(account, **changes):
    return TexmlEvent.model_validate(
        {
            "AccountSid": account,
            "ConnectionId": "123",
            "CallSid": "v3:first-call",
            "From": "+14155550101",
            "To": "+14155550102",
            **changes,
        }
    )


def test_concurrent_retries_create_one_call_and_event():
    async def scenario():
        async with database() as (pool, account):
            repo = VoiceRepository(pool)
            payload = event(account)

            calls = await asyncio.gather(
                *(repo.record(payload, "voice") for _ in range(12))
            )

            assert len({call.id for call in calls}) == 1

            count = await pool.fetchval(
                "SELECT count(*) FROM public.voice_call_events WHERE call_id = $1",
                calls[0].id,
            )
            assert count == 1

            # The same caller can make a separate call.
            other = await repo.record(
                event(account, CallSid="v3:second-call"),
                "voice",
            )
            assert other.id != calls[0].id

    asyncio.run(scenario())


def test_terminal_callback_before_voice_never_reopens_call():
    async def scenario():
        async with database() as (pool, account):
            repo = VoiceRepository(pool)

            completed = event(
                account,
                CallStatus="completed",
                SequenceNumber="3",
                CallbackSource="call-progress-events",
            )
            call = await repo.record(completed, "status")

            ended_at = await pool.fetchval(
                "SELECT ended_at FROM public.voice_calls WHERE id = $1",
                call.id,
            )
            assert ended_at is not None

            assert (await repo.record(event(account), "voice")).status == "completed"

            answered = event(
                account,
                CallStatus="in-progress",
                SequenceNumber="2",
                CallbackSource="call-progress-events",
            )
            assert (await repo.record(answered, "status")).status == "completed"

            await repo.record(completed, "status")  # Harmless retry.

            saved = await pool.fetchrow(
                "SELECT * FROM public.voice_calls WHERE id = $1",
                call.id,
            )
            assert saved["voice_requested"] is True
            assert saved["ended_at"] == ended_at

            count = await pool.fetchval(
                "SELECT count(*) FROM public.voice_call_events WHERE call_id = $1",
                call.id,
            )
            assert count == 3

    asyncio.run(scenario())


def test_out_of_order_status_and_conflicting_retry():
    async def scenario():
        async with database() as (pool, account):
            repo = VoiceRepository(pool)

            answered = event(
                account,
                CallStatus="in-progress",
                SequenceNumber="2",
                CallbackSource="call-progress-events",
            )
            call = await repo.record(answered, "status")

            ringing = event(
                account,
                CallStatus="ringing",
                SequenceNumber="1",
                CallbackSource="call-progress-events",
            )
            assert (await repo.record(ringing, "status")).status == "in-progress"

            # The same event identity cannot change its meaning on a retry.
            conflicting = answered.model_copy(update={"status": "failed"})
            with pytest.raises(VoiceEventConflict):
                await repo.record(conflicting, "status")

            status = await pool.fetchval(
                "SELECT status FROM public.voice_calls WHERE id = $1",
                call.id,
            )
            assert status == "in-progress"

            count = await pool.fetchval(
                "SELECT count(*) FROM public.voice_call_events WHERE call_id = $1",
                call.id,
            )
            assert count == 2

    asyncio.run(scenario())
