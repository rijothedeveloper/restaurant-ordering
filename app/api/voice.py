import asyncio
from typing import Annotated, Literal
from xml.etree.ElementTree import Element, SubElement, tostring

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket

from app.repositories.relay import RelayRepository
from app.repositories.voice import VoiceCall, VoiceEventConflict, VoiceRepository
from app.schemas.voice import TERMINAL_STATUSES, TexmlEvent
from app.services.relay import relay_configuration, serve_relay
from app.services.telnyx import verified_texml_event

router = APIRouter(prefix="/telnyx", tags=["voice"])

VerifiedEvent = Annotated[TexmlEvent, Depends(verified_texml_event)]


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
    settings = request.app.state.settings
    relay_url, secret = relay_configuration(settings)

    if event.direction != "inbound":
        raise HTTPException(status_code=403, detail="Expected an inbound voice call")

    # This still uses your working Lesson 2 signature dependency and persistence.
    call = await record_event(request, event, "voice")
    root = Element("Response")

    if call.status not in TERMINAL_STATUSES:
        try:
            async with asyncio.timeout(5):
                token = await RelayRepository(request.app.state.db_pool).issue(
                    call.id, secret
                )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
        ):
            raise HTTPException(503, "Relay database is unavailable") from None

        if token is not None:
            connect = SubElement(root, "Connect")

            relay = SubElement(
                connect,
                "ConversationRelay",
                url=relay_url,
                voice=settings.telnyx_relay_voice,
                language="en",
                transcriptionProvider=settings.telnyx_transcription_provider,
                interruptible="any",
                dtmfDetection="true",
                events="speaker-events tokens-played",
                partialPrompts="false",
            )

            # Telnyx includes this in the initial setup.customParameters.
            SubElement(
                relay,
                "Parameter",
                name="relay_token",
                value=token,
            )

            # These instructions execute after Conversation Relay stops.
            # SubElement(
            #     root,
            #     "Say",
            #     voice=settings.telnyx_relay_voice,
            #     language="en-US",
            # ).text = "The voice connection test has ended. Goodbye."

    SubElement(root, "Hangup")
    return xml_response(root)


@router.post("/status", response_class=Response)
async def call_status(request: Request, event: VerifiedEvent):
    await record_event(request, event, "status")

    # HTTP 200 acknowledges successful persistence.
    return xml_response(Element("Response"))


@router.websocket("/relay")
async def conversation_relay(websocket: WebSocket):
    await serve_relay(websocket)
