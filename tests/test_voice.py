from collections.abc import Iterator
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from app.api.voice import GREETING, create_voice_demo_app
from app.config import Settings


@pytest.fixture
def voice_settings() -> Settings:
    # The demo never connects to this database. Use explicit local test settings.
    return Settings(
        _env_file=None,
        environment="local",
        database_url="postgresql://test:test@127.0.0.1/unused",
    )


@pytest.fixture
def voice_client(voice_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_voice_demo_app(voice_settings)) as client:
        yield client


def test_incoming_call_returns_greeting_then_hangup(
    voice_client: TestClient,
) -> None:
    # The greeting ignores incoming form data and returns fixed instructions.
    response = voice_client.post(
        "/telnyx/voice",
        data={
            "From": "+12025550101",
            "To": "+12025550102",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].split(";", 1)[0] == "application/xml"
    assert response.headers["cache-control"] == "no-store"

    # Parse the returned XML and check the instructions in execution order.
    root = ElementTree.fromstring(response.content)
    assert root.tag == "Response"
    assert [child.tag for child in root] == ["Say", "Hangup"]
    assert root[0].text == GREETING
    assert root[0].attrib["language"] == "en-US"
    assert "+12025550101" not in response.text


def test_repeated_requests_return_the_same_instructions(
    voice_client: TestClient,
) -> None:
    # Repeated delivery must not add call actions to this static response.
    first = voice_client.post("/telnyx/voice")
    second = voice_client.post("/telnyx/voice")

    assert first.status_code == second.status_code == 200
    assert first.content == second.content


@pytest.mark.parametrize(
    "path",
    ["/", "/docs", "/redoc", "/openapi.json", "/menu", "/staff/me"],
)
def test_other_routes_are_unavailable(
    voice_client: TestClient,
    path: str,
) -> None:
    assert voice_client.get(path).status_code == 404


def test_webhook_requires_post(voice_client: TestClient) -> None:
    assert voice_client.get("/telnyx/voice").status_code == 405


def test_webhook_path_does_not_redirect(voice_client: TestClient) -> None:
    response = voice_client.post("/telnyx/voice/", follow_redirects=False)
    assert response.status_code == 404


@pytest.mark.parametrize("environment", ["test", "staging", "production"])
def test_demo_refuses_nonlocal_environments(
    voice_settings: Settings,
    environment: str,
) -> None:
    # Isolate the factory guard from unrelated production-settings validators.
    settings = voice_settings.model_copy(update={"environment": environment})

    with pytest.raises(RuntimeError, match="requires environment='local'"):
        create_voice_demo_app(settings)
