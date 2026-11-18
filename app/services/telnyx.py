import base64
import binascii
import re
import time
from urllib.parse import parse_qsl
import hashlib
import logging

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, Request
from pydantic import ValidationError

from app.schemas.voice import PHONE_PATTERN, TexmlEvent

MAX_BODY_BYTES = 64 * 1024
MAX_AGE_SECONDS = 300


def log_call_id_tags(stage: str, values: dict, names: tuple[str, ...]) -> None:
    tags = {}
    for name in names:
        value = values.get(name)
        tags[name] = (
            hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
            if isinstance(value, str) and value else "-"
        )
    logging.getLogger("uvicorn.error").info("Relay ID tags %s %s", stage, tags)


async def verified_texml_event(request: Request) -> TexmlEvent:
    settings = request.app.state.settings

    expected = (
        settings.telnyx_account_id,
        settings.telnyx_connection_id,
        settings.telnyx_phone_number,
    )

    if not settings.telnyx_public_key or not all(expected):
        raise HTTPException(503, "Voice configuration is incomplete")

    if not re.fullmatch(PHONE_PATTERN, settings.telnyx_phone_number):
        raise HTTPException(503, "Voice configuration is invalid")

    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(settings.telnyx_public_key, validate=True)
        )
    except (ValueError, binascii.Error):
        raise HTTPException(503, "Voice public key is invalid") from None

    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Expected form-encoded TeXML")

    # Preserve the original bytes and enforce a bounded request size.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "Webhook body is too large")
        body.extend(chunk)

    timestamps = request.headers.getlist("telnyx-timestamp")
    signatures = request.headers.getlist("telnyx-signature-ed25519")

    try:
        # Reject missing or duplicate signature headers.
        if len(timestamps) != 1 or len(signatures) != 1:
            raise ValueError

        timestamp = timestamps[0]
        if not re.fullmatch(r"[0-9]{1,12}", timestamp):
            raise ValueError

        # Our five-minute policy rejects stale and far-future signatures.
        if abs(time.time() - int(timestamp)) > MAX_AGE_SECONDS:
            raise ValueError

        signature = base64.b64decode(signatures[0], validate=True)

        public_key.verify(
            signature,
            timestamp.encode("ascii") + b"|" + bytes(body),
        )
    except (ValueError, binascii.Error, InvalidSignature):
        raise HTTPException(403, "Invalid Telnyx signature") from None

    # Only parse the form after its signature has been verified.
    try:
        if re.search(rb"%(?![0-9a-fA-F]{2})", body):
            raise ValueError

        pairs = parse_qsl(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=64,
        )

        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError  # Duplicate fields make the payload ambiguous.

        fields = dict(pairs)
        event = TexmlEvent.model_validate(fields)
        if request.url.path == "/telnyx/voice":
            log_call_id_tags(
                "voice",
                fields,
                ("CallSid", "CallSidLegacy", "CallLegId", "CallSessionId"),
            )
    except (ValueError, UnicodeError, ValidationError):
        raise HTTPException(400, "Invalid TeXML form") from None

    if (event.account_sid, event.connection_id, event.to_phone) != expected:
        raise HTTPException(
            403,
            "Unexpected Telnyx account, application, or number",
        )

    return event
