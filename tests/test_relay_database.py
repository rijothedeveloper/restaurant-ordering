import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest

from app.repositories.relay import RelayRepository, token_hash
from app.repositories.voice import VoiceRepository
from app.schemas.relay import Setup
from app.schemas.voice import TexmlEvent

SECRET = b"test-only-secret-" * 4


@asynccontextmanager
async def database():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL checks")

    if urlsplit(dsn).path != "/restaurant_test":
        pytest.fail("Use the restaurant_test database")

    pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=4,
        command_timeout=5,
    )
    account = f"test-{uuid4()}"

    try:
        assert await pool.fetchval("SELECT current_database()") == "restaurant_test"

        event = TexmlEvent.model_validate(
            {
                "AccountSid": account,
                "ConnectionId": "123",
                "CallSid": "v3:test-call",
                "To": "+14155550102",
            }
        )
        call = await VoiceRepository(pool).record(event, "voice")

        yield pool, call, event
    finally:
        await pool.execute(
            "DELETE FROM public.voice_calls WHERE account_sid = $1",
            account,
        )
        await pool.close()


def setup(event, token, **changes):
    return Setup.model_validate(
        {
            "type": "setup",
            "sessionId": str(uuid4()),
            "accountSid": event.account_sid,
            "callSid": "test-call-session",
            "callControlId": event.call_sid,
            "to": event.to_phone,
            "direction": "inbound",
            "customParameters": {"relay_token": token},
            **changes,
        }
    )


def test_retries_reuse_credential_and_only_one_connection_claims_it():
    async def scenario():
        async with database() as (pool, call, event):
            repo = RelayRepository(pool)

            tokens = await asyncio.gather(
                *(repo.issue(call.id, SECRET) for _ in range(8))
            )
            assert len(set(tokens)) == 1
            assert tokens[0] is not None

            stored = await pool.fetchrow(
                "SELECT * FROM public.voice_relay_sessions WHERE call_id = $1",
                call.id,
            )
            assert stored["token_hash"] == token_hash(tokens[0])
            assert stored["token_hash"] != tokens[0]

            claims = await asyncio.gather(
                *(
                    repo.claim(setup(event, tokens[0], to=None, direction=None))
                    for _ in range(8)
                )
            )
            assert sum(item is not None for item in claims) == 1

            session_id = next(item for item in claims if item is not None)
            assert await repo.active(session_id)

            # A used credential cannot be replaced or reused.
            assert await repo.issue(call.id, SECRET) is None

            await repo.finish(session_id)
            assert not await repo.active(session_id)
            assert await repo.claim(setup(event, tokens[0])) is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"accountSid": "another-account"},
        {"callSid": "v3:test-call", "callControlId": "v3:another-call"},
        {"to": "+14155550999"},
        {"customParameters": {"relay_token": "z" * 43}},
    ],
)
def test_credential_must_match_verified_call(changes):
    async def scenario():
        async with database() as (pool, call, event):
            repo = RelayRepository(pool)
            token = await repo.issue(call.id, SECRET)

            assert await repo.claim(setup(event, token, **changes)) is None

            # A mismatched attempt must not consume the legitimate credential.
            assert await repo.claim(setup(event, token)) is not None

    asyncio.run(scenario())


def test_expired_credential_is_not_renewed():
    async def scenario():
        async with database() as (pool, call, event):
            repo = RelayRepository(pool)
            token = await repo.issue(call.id, SECRET)

            await pool.execute(
                """
                UPDATE public.voice_relay_sessions
                SET expires_at = now() - interval '1 second'
                WHERE call_id = $1
                """,
                call.id,
            )

            assert await repo.claim(setup(event, token)) is None
            assert await repo.issue(call.id, SECRET) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("connected", [False, True])
def test_hangup_blocks_setup_or_ends_active_session(connected):
    async def scenario():
        async with database() as (pool, call, event):
            repo = RelayRepository(pool)
            token = await repo.issue(call.id, SECRET)

            session_id = await repo.claim(setup(event, token)) if connected else None

            completed = event.model_copy(
                update={
                    "status": "completed",
                    "sequence": 3,
                    "callback_source": "call-progress-events",
                }
            )
            await VoiceRepository(pool).record(completed, "status")

            assert await repo.claim(setup(event, token)) is None
            assert await repo.issue(call.id, SECRET) is None

            if connected:
                assert not await repo.active(session_id)

    asyncio.run(scenario())
