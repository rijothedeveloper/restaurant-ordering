import asyncio
import json
import logging
from contextlib import suppress
from urllib.parse import urlsplit

import asyncpg
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette.websockets import WebSocketState

from app.repositories.relay import RelayRepository
from app.schemas.relay import INCOMING, Dtmf, Interrupt, Prompt, RelayError, Setup

from app.services.telnyx import log_call_id_tags
import re

logger = logging.getLogger("uvicorn.error")


# This connection test has a three-minute duration limit and a one-minute idle limit. The credential’s 60-second expiry applies to establishing the connection; it does not end an already authenticated session.


MAX_FRAME_BYTES = 16_384
SETUP_SECONDS = 5
IDLE_SECONDS = 60
DEMO_SECONDS = 180
POLL_SECONDS = 5

DB_ERRORS = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    TimeoutError,
)

WELCOME = (
    "This is an automated voice connection test. "
    "Say something or press 1. Press pound to finish. "
    "This test cannot take orders."
)

REPLY = "Your speech reached the restaurant app. This is still a connection test."


def relay_configuration(settings) -> tuple[str, bytes]:
    url = settings.telnyx_relay_url or ""
    key = settings.telnyx_relay_secret

    try:
        parsed = urlsplit(url)
    except ValueError:
        raise HTTPException(503, "Relay URL is invalid") from None

    if (
        parsed.scheme != "wss"
        or not parsed.hostname
        or parsed.path != "/telnyx/relay"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or key is None
        or len(key.get_secret_value()) < 32
    ):
        raise HTTPException(503, "Relay configuration is incomplete or invalid")

    return url, key.get_secret_value().encode()


async def read_text(socket: WebSocket) -> str:
    message = await socket.receive()

    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))

    text = message.get("text")
    if text is None or len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ValueError("Expected a bounded text frame")

    return text


async def speak(socket: WebSocket, text: str) -> None:
    # "last" finishes this outgoing text turn.
    await socket.send_json(
        {
            "type": "text",
            "token": text,
            "last": True,
        }
    )

async def say_goodbye(socket: WebSocket) -> None:
    text = "The voice connection test has ended. Goodbye."
    await speak(socket, text)

    expected = re.sub(r"\W+", "", text.casefold())
    played = ""

    try:
        async with asyncio.timeout(8):
            while True:
                event = INCOMING.validate_json(await read_text(socket))

                if event.type == "error":
                    logger.warning("Relay reported an error during goodbye playback")
                    return

                if event.type != "info":
                    continue

                if event.name == "tokensPlayed":
                    chunk = re.sub(r"\W+", "", event.value.casefold())
                    played = (played + chunk)[-len(expected):]
                elif (
                    event.name == "agentSpeaking"
                    and event.value == "off"
                    and played == expected
                ):
                    return
    except TimeoutError:
        logger.warning("Goodbye playback confirmation timed out")


async def end_session(socket: WebSocket, reason: str) -> None:
    await socket.send_json(
        {
            "type": "end",
            "handoffData": json.dumps({"reason": reason}),
        }
    )


async def serve_relay(socket: WebSocket) -> None:
    pool = socket.app.state.db_pool
    if pool is None:
        await socket.close(code=1013)
        return

    repository = RelayRepository(pool)
    session_id = None
    close_code = 1000

    await socket.accept()

    try:
        # Accepting the transport does not authorize the session.
        # The first message must supply the credential within five seconds.
        raw_setup = await asyncio.wait_for(read_text(socket), SETUP_SECONDS)
        data = json.loads(raw_setup)

        if isinstance(data, dict) and data.get("type") == "setup":
            log_call_id_tags(
                "setup",
                data,
                ("callSid", "callControlId", "callLegId", "callSessionId"),
            )
            to = data.get("to")
            direction = data.get("direction")
            known_directions = (
                None,
                "",
                "inbound",
                "incoming",
                "outbound",
                "outgoing",
                "outbound-api",
                "outbound-dial",
                "unknown",
            )
            number_keys = (
                sorted(to.keys() & {"phone_number", "phoneNumber", "number"})
                if isinstance(to, dict)
                else []
            )
            logger.info(
                "Relay setup fields to_type=%s number_keys=%s "
                "direction_type=%s direction=%r",
                type(to).__name__ if "to" in data else "missing",
                number_keys,
                type(direction).__name__ if "direction" in data else "missing",
                direction if direction in known_directions else "<other>",
            )

        setup = Setup.model_validate_json(raw_setup)

        session_id = await asyncio.wait_for(repository.claim(setup), 3)
        if session_id is None:
            logger.warning(
                "Relay authorization rejected: credential or call checks failed"
            )
            close_code = 1008
            return

        # Log only our internal session identifier.
        logger.info("Relay authorized session=%s", session_id)

        async with asyncio.timeout(DEMO_SECONDS):
            await speak(socket, WELCOME)

            loop = asyncio.get_running_loop()
            last_message = loop.time()
            next_check = loop.time() + POLL_SECONDS
            frames = 0

            while True:
                # Notice a hangup recorded by another request or app instance.
                if loop.time() >= next_check:
                    active = await asyncio.wait_for(
                        repository.active(session_id),
                        3,
                    )
                    if not active:
                        await end_session(socket, "call_ended")
                        return

                    next_check = loop.time() + POLL_SECONDS

                try:
                    raw = await asyncio.wait_for(
                        read_text(socket),
                        min(POLL_SECONDS, IDLE_SECONDS),
                    )
                except TimeoutError:
                    if loop.time() - last_message >= IDLE_SECONDS:
                        await end_session(socket, "idle_timeout")
                        return
                    continue

                frame = INCOMING.validate_json(raw)
                last_message = loop.time()
                frames += 1

                if frames > 300:
                    raise ValueError("Demo message limit exceeded")

                if isinstance(frame, Prompt):
                    # Interim transcripts must not trigger actions.
                    if frame.last and frame.voice_prompt.strip():
                        await speak(socket, REPLY)

                elif isinstance(frame, Dtmf):
                    finish_key = frame.digit == "#"

                    logger.info("Relay keypad finish_key=%s", finish_key)

                    if finish_key:
                        await say_goodbye(socket)
                        await end_session(socket, "demo_complete")
                        logger.info("Relay end frame sent")

                        try:
                            async with asyncio.timeout(5):
                                while True:
                                    reply = INCOMING.validate_json(
                                        await read_text(socket)
                                    )
                                    if isinstance(reply, RelayError):
                                        logger.warning(
                                            "Relay after end: provider sent an error"
                                        )
                                        break
                        except WebSocketDisconnect as exc:
                            logger.info(
                                "Relay after end: peer closed, code=%s", exc.code
                            )
                        except TimeoutError:
                            logger.info(
                                "Relay after end: peer still connected after 5 seconds"
                            )
                        return

                    await speak(socket, "Keypad input received.")

                elif isinstance(frame, Interrupt):
                    # Telnyx stops playback; wait for the next final prompt.
                    continue

                elif isinstance(frame, RelayError):
                    logger.warning(
                        "Relay provider error session=%s",
                        session_id,
                    )
                    close_code = 1011
                    return

    except WebSocketDisconnect as exc:
        logger.warning(
            "Relay peer disconnected: code=%s authorized=%s",
            exc.code,
            session_id is not None,
        )

    except ValidationError as exc:
        # Report field names and error types without logging input values.

        problems = [
            {"field": error["loc"], "error": error["type"]}
            for error in exc.errors(
                include_input=False,
                include_context=False,
                include_url=False,
            )
        ]

        logger.warning("Relay validation failed: %s", problems)

        close_code = 1008

    except ValueError:
        logger.warning("Relay rejected: invalid frame or message limit")

        close_code = 1008

    except TimeoutError:
        logger.warning(
            "Relay timeout: authorized=%s",
            session_id is not None,
        )
        close_code = 1008 if session_id is None else 1000

        if session_id is not None:
            with suppress(WebSocketDisconnect, OSError, TimeoutError):
                await asyncio.wait_for(
                    end_session(socket, "timeout"),
                    1,
                )

    except DB_ERRORS:
        logger.warning("Relay persistence unavailable")
        close_code = 1011

    finally:
        if socket.client_state != WebSocketState.DISCONNECTED:
            with suppress(
                WebSocketDisconnect,
                RuntimeError,
                OSError,
                TimeoutError,
            ):
                await asyncio.wait_for(
                    socket.close(code=close_code),
                    1,
                )

        if session_id is not None:
            try:
                await asyncio.wait_for(repository.finish(session_id), 3)
            except DB_ERRORS:
                logger.warning(
                    "Relay cleanup unavailable session=%s",
                    session_id,
                )
