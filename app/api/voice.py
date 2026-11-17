"""Phase 8, Lesson 1: a Telnyx greeting endpoint."""

from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, FastAPI, Response

from app.config import Settings, get_settings

router = APIRouter(prefix="/telnyx", tags=["voice"])

GREETING = (
    "Hello. You have reached the restaurant ordering test line. "
    "The phone connection is working. "
    "This test line cannot take orders yet. Goodbye."
)


@router.post("/voice", response_class=Response)
async def incoming_voice() -> Response:
    # Build Telnyx TeXML using Python's standard library.
    root = Element("Response")
    SubElement(root, "Say", voice="man", language="en-US").text = GREETING
    SubElement(root, "Hangup")

    # Return XML bytes directly; no provider SDK or API key is needed here.
    return Response(
        content=tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
        headers={"Cache-Control": "no-store"},
    )


def create_voice_demo_app(settings: Settings | None = None) -> FastAPI:
    """Run only the greeting endpoint during the initial tunnel test."""
    settings = settings if settings is not None else get_settings()

    # Request verification and integration into the main app follow in Lesson 2.
    if settings.environment != "local":
        raise RuntimeError("The voice demo requires environment='local'.")

    app = FastAPI(
        title="Restaurant Telnyx voice demo",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.include_router(router)
    return app
