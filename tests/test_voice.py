import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import asyncpg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.voice import router
from app.repositories.relay import RelayRepository
from app.repositories.voice import VoiceCall, VoiceEventConflict, VoiceRepository
from app.services.telnyx import MAX_BODY_BYTES

FORM = {
    "AccountSid": "test-account",
    "ConnectionId": "1234567890",
    "CallSid": "v3:opaque-call-id",
    "From": "+14155550101",
    "To": "+14155550102",
    "Direction": "inbound",
}

STATUS = {
    **FORM,
    "CallStatus": "completed",
    "CallbackSource": "call-progress-events",
    "SequenceNumber": "3",
}


def signed_headers(key, body, timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = key.sign(timestamp.encode() + b"|" + body)

    return {
        "content-type": "application/x-www-form-urlencoded",
        "telnyx-timestamp": timestamp,
        "telnyx-signature-ed25519": base64.b64encode(signature).decode(),
    }


@pytest.fixture
def harness(monkeypatch):
    key = Ed25519PrivateKey.generate()

    app = FastAPI()
    app.state.settings = SimpleNamespace(
        telnyx_public_key=base64.b64encode(
            key.public_key().public_bytes_raw()
        ).decode(),
        telnyx_account_id=FORM["AccountSid"],
        telnyx_connection_id=FORM["ConnectionId"],
        telnyx_phone_number=FORM["To"],
        telnyx_relay_url="wss://voice.example.com/telnyx/relay",
        telnyx_relay_secret=SecretStr("test-secret-" * 4),
        telnyx_relay_voice="Telnyx.Natural.abbie",
        telnyx_transcription_provider="deepgram",
    )

    app.state.db_pool = object()

    recorder = AsyncMock(return_value=VoiceCall(uuid4(), "received"))
    monkeypatch.setattr(VoiceRepository, "record", recorder)

    monkeypatch.setattr(
        RelayRepository,
        "issue",
        AsyncMock(return_value="t" * 43),
    )

    app.include_router(router)

    with TestClient(app) as client:
        yield client, app, key, recorder


def post_signed(harness, form, path="/telnyx/voice"):
    client, _, key, _ = harness
    body = urlencode(form).encode()
    return client.post(path, content=body, headers=signed_headers(key, body))


def test_verified_voice_returns_relay_and_records(harness):
    response = post_signed(harness, FORM)

    assert response.status_code == 200

    root = fromstring(response.content)
    assert [child.tag for child in root] == ["Connect", "Say", "Hangup"]

    relay = root.find("Connect/ConversationRelay")
    assert relay is not None
    assert relay.attrib["url"] == "wss://voice.example.com/telnyx/relay"

    parameter = relay.find("Parameter")
    assert parameter is not None
    assert parameter.attrib == {
        "name": "relay_token",
        "value": "t" * 43,
    }

    event, kind = harness[3].await_args.args
    assert (event.call_sid, event.to_phone, kind) == (
        FORM["CallSid"],
        FORM["To"],
        "voice",
    )


@pytest.mark.parametrize("caller", ["anonymous", "sip:caller@example.com", ""])
def test_withheld_or_non_phone_caller_is_allowed(harness, caller):
    assert post_signed(harness, {**FORM, "From": caller}).status_code == 200
    assert harness[3].await_args.args[0].from_phone is None


@pytest.mark.parametrize(
    "case",
    ["missing", "tampered", "wrong-key", "old", "future", "bad-base64", "duplicate"],
)
def test_invalid_signature_never_reaches_repository(harness, case):
    client, _, key, recorder = harness
    body = urlencode(FORM).encode()

    timestamp = int(time.time()) + {"old": -600, "future": 600}.get(case, 0)
    headers = signed_headers(key, body, timestamp)

    if case == "missing":
        headers.pop("telnyx-signature-ed25519")
    elif case == "tampered":
        body += b"&Extra=changed"
    elif case == "wrong-key":
        headers = signed_headers(Ed25519PrivateKey.generate(), body)
    elif case == "bad-base64":
        headers["telnyx-signature-ed25519"] = "!invalid!"
    elif case == "duplicate":
        headers = list(headers.items()) + [("telnyx-timestamp", str(timestamp))]

    response = client.post("/telnyx/voice", content=body, headers=headers)

    assert response.status_code == 403
    recorder.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("AccountSid", "another-account"),
        ("ConnectionId", "999"),
        ("To", "+14155550999"),
    ],
)
def test_signed_but_unexpected_identity_is_rejected(harness, field, value):
    assert post_signed(harness, {**FORM, field: value}).status_code == 403
    harness[3].assert_not_awaited()


@pytest.mark.parametrize(
    "extra",
    [b"&To=%2B14155550102", b"&Extra=%ZZ", b"&Extra=%FF"],
)
def test_signed_malformed_form_is_rejected(harness, extra):
    client, _, key, recorder = harness
    body = urlencode(FORM).encode() + extra

    response = client.post(
        "/telnyx/voice",
        content=body,
        headers=signed_headers(key, body),
    )

    assert response.status_code == 400
    recorder.assert_not_awaited()


def test_signature_uses_original_bytes(harness):
    client, _, key, recorder = harness
    body = urlencode(FORM).encode()

    # Both forms decode identically, but their signed bytes differ.
    altered = body.replace(b"%2B", b"%2b")
    assert altered != body

    response = client.post(
        "/telnyx/voice",
        content=altered,
        headers=signed_headers(key, body),
    )

    assert response.status_code == 403
    recorder.assert_not_awaited()


@pytest.mark.parametrize(
    "path,form",
    [
        ("/telnyx/voice", STATUS),
        ("/telnyx/status", FORM),
        ("/telnyx/status", {**STATUS, "CallStatus": "unknown"}),
        (
            "/telnyx/status",
            {key: value for key, value in STATUS.items() if key != "SequenceNumber"},
        ),
    ],
)
def test_wrong_callback_purpose_is_rejected(harness, path, form):
    assert post_signed(harness, form, path).status_code == 400
    harness[3].assert_not_awaited()


def test_status_is_acknowledged_without_voice_instructions(harness):
    response = post_signed(harness, STATUS, "/telnyx/status")

    assert response.status_code == 200
    assert len(fromstring(response.content)) == 0
    assert harness[3].await_args.args[1] == "status"


def test_ended_call_cannot_be_greeted_again(harness):
    harness[3].return_value = VoiceCall(uuid4(), "completed")

    response = post_signed(harness, FORM)

    assert response.status_code == 200
    assert [child.tag for child in fromstring(response.content)] == ["Hangup"]


@pytest.mark.parametrize(
    "error,code",
    [
        (VoiceEventConflict(), 409),
        (asyncpg.PostgresError("test failure"), 503),
    ],
)
def test_persistence_errors_are_not_acknowledged(harness, error, code):
    harness[3].side_effect = error
    assert post_signed(harness, FORM).status_code == code


def test_unconfigured_voice_fails_closed(harness):
    harness[1].state.settings.telnyx_public_key = None

    assert post_signed(harness, FORM).status_code == 503
    harness[3].assert_not_awaited()


def test_content_type_and_body_limit(harness):
    client, _, key, recorder = harness

    assert client.post("/telnyx/voice", json=FORM).status_code == 415

    body = b"x" * (MAX_BODY_BYTES + 1)
    response = client.post(
        "/telnyx/voice",
        content=body,
        headers=signed_headers(key, body),
    )

    assert response.status_code == 413
    recorder.assert_not_awaited()
