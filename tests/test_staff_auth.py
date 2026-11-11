import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import firebase_admin
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from firebase_admin import _auth_client, _token_gen, auth, credentials
from google.auth import crypt, jwt
from google.auth.credentials import AnonymousCredentials
from google.auth.exceptions import DefaultCredentialsError, TransportError
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app

PROJECT_ID = "restaurant-auth-test"
USER_ID = "firebase-staff-user"


def settings(**changes):
    values = {
        "_env_file": None,
        "environment": "test",
        "database_url": "postgres://unused:unused@localhost/restaurant_test",
        "firebase_project_id": PROJECT_ID,
        "staff_web_origin": "http://localhost:3000",
    }
    return Settings(**(values | changes))


class TestCredential(credentials.Base):
    def get_credential(self):
        return AnonymousCredentials()


@pytest.fixture(scope="module")
def keys():
    return [
        rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)
    ]


def token(keys, *, changes=None, missing=None, forged=False, kid="test-key"):
    now = int(time.time())
    claims = {
        "iss": f"https://securetoken.google.com/{PROJECT_ID}",
        "aud": PROJECT_ID,
        "sub": USER_ID,
        "iat": now - 10,
        "exp": now + 3600,
        "auth_time": now - 10,
        "staff": True,
    }

    claims.update(changes or {})

    if missing:
        claims.pop(missing)

    pem = keys[int(forged)].private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    signer = crypt.RSASigner.from_string(pem, key_id=kid)
    return jwt.encode(signer, claims).decode()


@pytest.fixture
def user_record():
    return SimpleNamespace(
        disabled=False,
        tokens_valid_after_timestamp=0,
    )


@pytest.fixture
def pool_factory(monkeypatch):
    factory = AsyncMock(return_value=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr("app.db.pool.asyncpg.create_pool", factory)
    return factory


@pytest.fixture
def firebase_boundary(monkeypatch, keys, user_record):
    monkeypatch.delenv("FIREBASE_AUTH_EMULATOR_HOST", raising=False)
    monkeypatch.setattr(credentials, "ApplicationDefault", TestCredential)

    public_key = (
        keys[0]
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    certificates = json.dumps({"test-key": public_key.decode()}).encode()

    monkeypatch.setattr(
        _token_gen.CertificateFetchRequest,
        "__call__",
        lambda self, *args, **kwargs: SimpleNamespace(
            status=200,
            data=certificates,
        ),
    )
    monkeypatch.setattr(
        _auth_client.Client,
        "get_user",
        lambda self, uid: user_record,
    )


@pytest.fixture
def client(firebase_boundary, pool_factory):
    # Real Firebase token verification; only external I/O is replaced.
    with TestClient(create_app(settings())) as test_client:
        yield test_client


def request(client, value):
    return client.get(
        "/staff/me",
        headers={"Authorization": f"Bearer {value}"},
    )


def test_staff_identity(client, keys):
    response = request(client, token(keys))

    assert response.status_code == 200
    assert response.json() == {
        "user_id": USER_ID,
        "role": "staff",
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "header",
    [None, "Basic abc", "Bearer", "Bearer malformed"],
)
def test_requires_valid_bearer_token(client, header):
    headers = {} if header is None else {"Authorization": header}
    response = client.get("/staff/me", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"iss": "https://untrusted.example"}, 401),
        ({"aud": "another-project"}, 401),
        ({"exp": 1}, 401),
        ({"iat": 4102444800}, 401),
        ({"sub": ""}, 401),
        ({"staff": False}, 403),
        ({"staff": "true"}, 403),
        ({"staff": 1}, 403),
    ],
)
def test_rejects_wrong_claims(client, keys, changes, status):
    response = request(client, token(keys, changes=changes))

    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "claim",
    ["exp", "iat", "sub", "iss", "aud"],
)
def test_required_claims(client, keys, claim):
    response = request(client, token(keys, missing=claim))
    assert response.status_code == 401


def test_unapproved_user_is_forbidden(client, keys):
    response = request(client, token(keys, missing="staff"))
    assert response.status_code == 403


@pytest.mark.parametrize(
    "changes",
    [{"forged": True}, {"kid": "unknown"}],
)
def test_untrusted_signature(client, keys, changes):
    response = request(client, token(keys, **changes))
    assert response.status_code == 401


@pytest.mark.parametrize(
    "disabled,revoked",
    [(True, False), (False, True)],
)
def test_disabled_or_revoked_session(client, keys, user_record, disabled, revoked):
    user_record.disabled = disabled

    if revoked:
        user_record.tokens_valid_after_timestamp = int(time.time()) * 1000

    assert request(client, token(keys)).status_code == 401


def test_deleted_user(client, keys, monkeypatch):
    def missing(self, uid):
        raise auth.UserNotFoundError("User deleted")

    monkeypatch.setattr(_auth_client.Client, "get_user", missing)
    assert request(client, token(keys)).status_code == 401


@pytest.mark.parametrize(
    "source",
    ["certificates", "credentials", "user_service"],
)
def test_provider_failure_is_503(client, keys, monkeypatch, source):
    def fail(*args, **kwargs):
        if source == "credentials":
            raise DefaultCredentialsError("private credential detail")
        if source == "user_service":
            raise firebase_admin.exceptions.UnavailableError("private service detail")
        raise TransportError("private network detail")

    if source == "credentials":
        monkeypatch.setattr(TestCredential, "get_credential", fail)
    elif source == "user_service":
        monkeypatch.setattr(_auth_client.Client, "get_user", fail)
    else:
        monkeypatch.setattr(_token_gen.CertificateFetchRequest, "__call__", fail)

    response = request(client, token(keys))

    assert response.status_code == 503
    assert response.json() == {"detail": "Staff authentication temporarily unavailable"}


def test_unconfigured_auth_fails_closed(pool_factory, keys):
    app = create_app(settings(firebase_project_id=None))

    with TestClient(app) as client:
        assert request(client, token(keys)).status_code == 503
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"firebase_project_id": "invalid project"},
        {"staff_web_origin": "http://localhost:3000/path"},
        {"staff_web_origin": "http://user:password@localhost:3000"},
    ],
)
def test_invalid_configuration(changes):
    with pytest.raises(ValidationError):
        settings(**changes)


@pytest.mark.parametrize(
    "origin,status",
    [
        ("http://localhost:3000", 200),
        ("https://untrusted.example", 400),
    ],
)
def test_cors(client, origin, status):
    response = client.options(
        "/staff/me",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert response.status_code == status

    if status == 200:
        assert response.headers["access-control-allow-origin"] == origin
        assert (
            client.get(
                "/staff/me",
                headers={"Origin": origin},
            ).status_code
            == 401
        )
    else:
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("startup_fails", [False, True])
def test_firebase_app_cleanup(
    firebase_boundary, pool_factory, monkeypatch, startup_fails
):
    deleted = Mock(wraps=firebase_admin.delete_app)
    monkeypatch.setattr(firebase_admin, "delete_app", deleted)
    app = create_app(settings())

    if startup_fails:
        pool_factory.side_effect = OSError("Database unavailable")

        with pytest.raises(OSError), TestClient(app):
            pass
    else:
        with TestClient(app):
            assert app.state.firebase_app is not None

    assert app.state.firebase_app is None
    deleted.assert_called_once()

    with pytest.raises(ValueError):
        firebase_admin.get_app(deleted.call_args.args[0].name)


def test_emulator_cannot_bypass_verification(monkeypatch):
    monkeypatch.setenv(
        "FIREBASE_AUTH_EMULATOR_HOST",
        "localhost:9099",
    )

    with (
        pytest.raises(RuntimeError, match="emulator"),
        TestClient(create_app(settings())),
    ):
        pass
