import asyncio
from typing import Annotated, Literal
from xml.etree.ElementTree import Element, SubElement, tostring

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.repositories.voice import VoiceCall, VoiceEventConflict, VoiceRepository
from app.schemas.voice import TERMINAL_STATUSES, TexmlEvent
from app.services.telnyx import verified_texml_event


router = APIRouter(prefix="/telnyx", tags=["voice"])

VerifiedEvent = Annotated[TexmlEvent, Depends(verified_texml_event)]

GREETING = (
    "Hello. You have reached the restaurant ordering test line. "
    "The phone connection is working. "
    "This test line cannot take orders yet. Goodbye."
)


async def record_event(
    request: Request,
    event: TexmlEvent,
    kind: Literal["voice", "status"],
) -> VoiceCall:
    # Signatures bind the body, not the URL. Check the payload's purpose too.
    if kind == "voice":
        valid = event.callback_source is None and event.sequence is None
    else:
        valid = (
            event.callback_source == "call-progress-events"
            and event.sequence is not None
            and event.status != "received"
        )

    if not valid:
        raise HTTPException(400, "Unexpected TeXML callback type")

    pool = request.app.state.db_pool
    if pool is None:
        raise HTTPException(503, "Voice database is unavailable")

    try:
        async with asyncio.timeout(5):
            return await VoiceRepository(pool).record(event, kind)
    except VoiceEventConflict:
        raise HTTPException(409, "Conflicting Telnyx event") from None
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
        # A failed write must not receive a successful acknowledgement.
        raise HTTPException(503, "Voice database is unavailable") from None


def xml_response(root: Element) -> Response:
    return Response(
        content=tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/voice", response_class=Response)
async def incoming_voice(request: Request, event: VerifiedEvent):
    call = await record_event(request, event, "voice")

    root = Element("Response")
    if call.status not in TERMINAL_STATUSES:
        SubElement(root, "Say", voice="man", language="en-US").text = GREETING

    SubElement(root, "Hangup")
    return xml_response(root)


@router.post("/status", response_class=Response)
async def call_status(request: Request, event: VerifiedEvent):
    await record_event(request, event, "status")

    # HTTP 200 acknowledges successful persistence.
    return xml_response(Element("Response"))
