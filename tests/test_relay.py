from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from app.api.voice import router
from app.repositories.relay import RelayRepository
from app.services import relay as service

SETUP = {
    "type": "setup",
    "sessionId": "provider-session",
    "accountSid": "account",
    "callSid": "test-call-session",
    "callControlId": "v3:call",
    "to": "+14155550102",
    "direction": "inbound",
    "customParameters": {"relay_token": "t" * 43},
}


@pytest.fixture
def harness(monkeypatch):
    app = FastAPI()
    app.state.db_pool = object()
    app.include_router(router)

    claim = AsyncMock(return_value=uuid4())
    active = AsyncMock(return_value=True)
    finish = AsyncMock()

    monkeypatch.setattr(RelayRepository, "claim", claim)
    monkeypatch.setattr(RelayRepository, "active", active)
    monkeypatch.setattr(RelayRepository, "finish", finish)

    with TestClient(app) as client:
        yield client, claim, active, finish


def start(socket):
    socket.send_json(SETUP)
    assert socket.receive_json() == {
        "type": "text",
        "token": service.WELCOME,
        "last": True,
    }


def test_final_speech_keypad_and_interrupt(harness):
    with harness[0].websocket_connect("/telnyx/relay") as socket:
        start(socket)

        socket.send_json(
            {
                "type": "prompt",
                "voicePrompt": "partial",
                "last": False,
            }
        )
        socket.send_json({"type": "dtmf", "digit": "1"})

        # A partial transcript must not queue a response.
        assert socket.receive_json()["token"] == "Keypad input received."

        socket.send_json(
            {
                "type": "interrupt",
                "utteranceUntilInterrupt": "Keypad",
                "durationUntilInterruptMs": 200,
            }
        )
        socket.send_json(
            {
                "type": "prompt",
                "voicePrompt": "hello",
                "last": True,
            }
        )

        assert socket.receive_json()["token"] == service.REPLY

        socket.send_json({"type": "dtmf", "digit": "#"})
        assert socket.receive_json()["type"] == "end"

        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1000

    harness[3].assert_awaited_once()


@pytest.mark.parametrize(
    "first",
    [
        {"type": "prompt", "voicePrompt": "hello", "last": True},
        {**SETUP, "customParameters": {}},
        {**SETUP, "customParameters": {"relay_token": "short"}},
    ],
)
def test_invalid_setup_never_reaches_database(harness, first):
    with harness[0].websocket_connect("/telnyx/relay") as socket:
        socket.send_json(first)

        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008

    harness[1].assert_not_awaited()


def test_unknown_expired_or_used_credential_is_rejected(harness):
    harness[1].return_value = None

    with harness[0].websocket_connect("/telnyx/relay") as socket:
        socket.send_json(SETUP)

        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008

    harness[3].assert_not_awaited()


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "prompt", "voicePrompt": "hello", "last": "true"},
        {"type": "dtmf", "digit": "12"},
        SETUP,
        {"type": "unknown"},
        {
            "type": "interrupt",
            "utteranceUntilInterrupt": "x",
            "durationUntilInterruptMs": -1,
        },
    ],
)
def test_invalid_followup_is_rejected(harness, frame):
    with harness[0].websocket_connect("/telnyx/relay") as socket:
        start(socket)
        socket.send_json(frame)

        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008

    harness[3].assert_awaited_once()


@pytest.mark.parametrize("case", ["binary", "oversized", "invalid-json"])
def test_invalid_transport_message(harness, case):
    with harness[0].websocket_connect("/telnyx/relay") as socket:
        if case == "binary":
            socket.send_bytes(b"audio is not accepted")
        elif case == "oversized":
            socket.send_text("x" * (service.MAX_FRAME_BYTES + 1))
        else:
            socket.send_text("{")

        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008


def test_setup_deadline(harness, monkeypatch):
    monkeypatch.setattr(service, "SETUP_SECONDS", 0.02)

    with harness[0].websocket_connect("/telnyx/relay") as socket:
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008


@pytest.mark.parametrize("case", ["idle", "ended-call", "duration"])
def test_session_limits_and_ended_call(harness, monkeypatch, case):
    monkeypatch.setattr(service, "POLL_SECONDS", 0.01)

    if case == "idle":
        monkeypatch.setattr(service, "IDLE_SECONDS", 0.02)
    elif case == "ended-call":
        harness[2].return_value = False
    else:
        monkeypatch.setattr(service, "DEMO_SECONDS", 0.02)

    with harness[0].websocket_connect("/telnyx/relay") as socket:
        start(socket)
        assert socket.receive_json()["type"] == "end"

        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()

    harness[3].assert_awaited_once()


def test_provider_error_closes_and_cleans_up(harness):
    with harness[0].websocket_connect("/telnyx/relay") as socket:
        start(socket)
        socket.send_json(
            {
                "type": "error",
                "description": "Test provider error",
            }
        )

        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1011

    harness[3].assert_awaited_once()


def test_database_failure_fails_closed(harness):
    harness[1].side_effect = asyncpg.PostgresError("Test failure")

    with harness[0].websocket_connect("/telnyx/relay") as socket:
        socket.send_json(SETUP)

        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1011
