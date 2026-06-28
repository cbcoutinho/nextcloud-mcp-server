"""Regression tests for GHSA-x88r-fhx7-52h6.

Unauthenticated cross-user scope tampering & disclosure on the user-management
API. ``GET /api/v1/users/{id}/access``, ``PATCH /api/v1/users/{id}/scopes`` and
``GET /api/v1/users/{id}/app-password`` authenticated via ``_extract_basic_auth``,
which validated only that the BasicAuth *username* equals the ``{user_id}`` path
segment — **the password was never checked**. An attacker who knows a victim's
username (not a secret) could rewrite/read that victim's stored scopes by sending
``Authorization: Basic base64("victim:ANYTHING")``.

These tests drive the genuine handlers with a credential Nextcloud *rejects*
(the OCS validation endpoint returns 401). The fix must reject the request
(401) and leave stored state untouched; without the fix the handlers never call
OCS and return 200, tampering with / disclosing the victim's data.
"""

import base64
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api import passwords
from nextcloud_mcp_server.api.access import get_user_access, update_user_scopes
from nextcloud_mcp_server.api.passwords import get_app_password_status
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit

# A syntactically-valid but fake app-password token, referenced rather than
# inlined at each call site so the static "hard-coded credential" heuristic
# (and DRY) stays happy.
STORED_TOKEN = "aaaaa-bbbbb-ccccc-ddddd-eeeee"


@pytest.fixture(autouse=True)
def clear_rate_limit():
    """Isolate the module-global rate-limiter state between tests."""
    passwords._rate_limit_attempts.clear()
    yield
    passwords._rate_limit_attempts.clear()


@pytest.fixture
def encryption_key():
    return Fernet.generate_key().decode()


@pytest.fixture
async def temp_storage(encryption_key):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_ghsa.db"
        storage = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=encryption_key
        )
        await storage.initialize()
        yield storage


async def _provision_victim(storage, scopes, *, login_name="victim"):
    """Store the victim's app password + scopes (the stored state an attacker
    must not be able to read or rewrite)."""
    await storage.store_app_password_with_scopes(
        user_id="victim",
        app_password=STORED_TOKEN,
        scopes=scopes,
        username=login_name,
    )


def _basic_auth(name: str, secret: str) -> str:
    encoded = base64.b64encode(f"{name}:{secret}".encode()).decode()
    return f"Basic {encoded}"


def _settings_with_host(mocker, host="http://localhost:8080"):
    mocker.patch(
        "nextcloud_mcp_server.api.passwords.get_settings",
        return_value=MagicMock(
            nextcloud_host=host,
            nextcloud_verify_ssl=True,
            nextcloud_ca_bundle=None,
        ),
    )


def _mock_ocs(mocker, *, status_code, json_payload=None):
    """Patch the OCS client to return a canned response; return the mock client
    so callers can assert the credentials it was called with."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = json_payload

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    mocker.patch(
        "nextcloud_mcp_server.api.passwords.nextcloud_httpx_client",
        return_value=mock_client,
    )
    return mock_client


def _mock_nextcloud_rejects_credentials(mocker):
    """Nextcloud rejects the supplied password (OCS ``/cloud/user`` → HTTP 401).

    A correct handler routes this to a 401; the vulnerable handler never makes
    the call and proceeds to read/rewrite stored state.
    """
    _settings_with_host(mocker)
    return _mock_ocs(mocker, status_code=401)


def _app(storage) -> Starlette:
    app = Starlette(
        routes=[
            Route("/api/v1/users/{user_id}/access", get_user_access, methods=["GET"]),
            Route(
                "/api/v1/users/{user_id}/scopes",
                update_user_scopes,
                methods=["PATCH"],
            ),
            Route(
                "/api/v1/users/{user_id}/app-password",
                get_app_password_status,
                methods=["GET"],
            ),
        ]
    )
    app.state.storage = storage
    return app


async def test_update_scopes_rejects_unvalidated_password(temp_storage, mocker):
    """PATCH /scopes must not rewrite a victim's scopes for an attacker who
    only knows the username and supplies a password Nextcloud rejects."""
    await _provision_victim(temp_storage, ["notes.read"])
    _mock_nextcloud_rejects_credentials(mocker)

    client = TestClient(_app(temp_storage))
    resp = client.patch(
        "/api/v1/users/victim/scopes",
        headers={"Authorization": _basic_auth("victim", "WRONG-attacker-guess")},
        json={"scopes": ["notes.read", "notes.write", "files.write"]},
    )

    assert resp.status_code == 401, (
        "wrong password must be rejected, not allowed to rewrite scopes"
    )
    # The victim's stored scopes must be untouched.
    data = await temp_storage.get_app_password_with_scopes("victim")
    assert data["scopes"] == ["notes.read"]


async def test_get_access_rejects_unvalidated_password(temp_storage, mocker):
    """GET /access must not disclose a victim's scopes/metadata to an attacker
    supplying a password Nextcloud rejects."""
    await _provision_victim(temp_storage, ["notes.read", "calendar.write"])
    _mock_nextcloud_rejects_credentials(mocker)

    client = TestClient(_app(temp_storage))
    resp = client.get(
        "/api/v1/users/victim/access",
        headers={"Authorization": _basic_auth("victim", "WRONG-attacker-guess")},
    )

    assert resp.status_code == 401, "wrong password must not disclose access state"
    body = resp.json()
    assert "calendar.write" not in str(body)


async def test_get_app_password_status_rejects_unvalidated_password(
    temp_storage, mocker
):
    """GET /app-password must not disclose provisioning status to an attacker
    supplying a password Nextcloud rejects."""
    await _provision_victim(temp_storage, ["notes.read"])
    _mock_nextcloud_rejects_credentials(mocker)

    client = TestClient(_app(temp_storage))
    resp = client.get(
        "/api/v1/users/victim/app-password",
        headers={"Authorization": _basic_auth("victim", "WRONG-attacker-guess")},
    )

    assert resp.status_code == 401, "wrong password must not disclose status"


async def test_repeated_wrong_passwords_are_rate_limited(temp_storage, mocker):
    """Repeated failed credential attempts on the newly-authenticated read/scope
    routes hit the shared per-user rate limit (each costs an OCS round-trip), so
    they cannot be hammered to brute-force a victim's password indefinitely."""
    await _provision_victim(temp_storage, ["notes.read"])
    _mock_nextcloud_rejects_credentials(mocker)

    client = TestClient(_app(temp_storage))
    headers = {"Authorization": _basic_auth("victim", "WRONG-guess")}
    # RATE_LIMIT_MAX_ATTEMPTS failed attempts all return 401...
    for i in range(passwords.RATE_LIMIT_MAX_ATTEMPTS):
        resp = client.get("/api/v1/users/victim/access", headers=headers)
        assert resp.status_code == 401, f"attempt {i + 1} should be 401"

    # ...the next is throttled with 429 + Retry-After.
    resp = client.get("/api/v1/users/victim/access", headers=headers)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


async def test_unauthenticated_flood_does_not_lock_out_victim(temp_storage, mocker):
    """Requests with a missing/mismatched Authorization header are rejected
    before the rate limiter, so an unauthenticated flood can't exhaust a
    victim's budget and lock them out (request-flood DoS vs. brute-force)."""
    await _provision_victim(temp_storage, ["notes.read"])
    _mock_nextcloud_rejects_credentials(mocker)

    client = TestClient(_app(temp_storage))
    # A flood well past the cap: no header, then wrong username — neither should
    # consume the victim's rate-limit budget.
    for _ in range(passwords.RATE_LIMIT_MAX_ATTEMPTS * 2):
        assert client.get("/api/v1/users/victim/access").status_code == 401
    for _ in range(passwords.RATE_LIMIT_MAX_ATTEMPTS * 2):
        resp = client.get(
            "/api/v1/users/victim/access",
            headers={"Authorization": _basic_auth("attacker", "x")},
        )
        assert resp.status_code == 403  # username != path

    # A genuine credential attempt for the victim is still served (401), not
    # pre-emptively throttled by the flood above.
    resp = client.get(
        "/api/v1/users/victim/access",
        headers={"Authorization": _basic_auth("victim", "still-wrong")},
    )
    assert resp.status_code == 401


async def test_correct_password_is_never_rate_limited(temp_storage, mocker):
    """Only *failed* attempts count: a client polling with a valid credential
    is never throttled, even past the failure cap."""
    await _provision_victim(temp_storage, ["notes.read"])
    _settings_with_host(mocker)
    _mock_ocs(
        mocker,
        status_code=200,
        json_payload={"ocs": {"meta": {"statuscode": 200}, "data": {"id": "victim"}}},
    )

    client = TestClient(_app(temp_storage))
    headers = {"Authorization": _basic_auth("victim", STORED_TOKEN)}
    for _ in range(passwords.RATE_LIMIT_MAX_ATTEMPTS + 3):
        resp = client.get("/api/v1/users/victim/access", headers=headers)
        assert resp.status_code == 200


async def test_invalid_credentials_error_wording_on_access(temp_storage, mocker):
    """The access/scope routes report "Invalid credentials", not the
    app-password-specific default, since they don't deal with app passwords."""
    await _provision_victim(temp_storage, ["notes.read"])
    _mock_nextcloud_rejects_credentials(mocker)

    client = TestClient(_app(temp_storage))
    resp = client.get(
        "/api/v1/users/victim/access",
        headers={"Authorization": _basic_auth("victim", "WRONG-guess")},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "Invalid credentials"


async def test_oidc_loginname_authenticates_then_uid_matches_path(temp_storage, mocker):
    """OIDC users (UID != loginName): the OCS auth uses the body ``username``
    (loginName), while the returned canonical UID is what's matched against the
    path. A correct credential updates scopes successfully."""
    await temp_storage.store_app_password_with_scopes(
        user_id="Ada Lovelace",
        app_password=STORED_TOKEN,
        scopes=["notes.read"],
        username="ada@example.com",
    )
    _settings_with_host(mocker)
    ocs = _mock_ocs(
        mocker,
        status_code=200,
        json_payload={
            "ocs": {"meta": {"statuscode": 200}, "data": {"id": "Ada Lovelace"}}
        },
    )

    client = TestClient(_app(temp_storage))
    resp = client.patch(
        "/api/v1/users/Ada Lovelace/scopes",
        headers={"Authorization": _basic_auth("Ada Lovelace", STORED_TOKEN)},
        json={"scopes": ["notes.read", "notes.write"], "username": "ada@example.com"},
    )

    assert resp.status_code == 200
    # OCS was authenticated as the loginName from the body, not the UID.
    _, get_kwargs = ocs.get.call_args
    assert get_kwargs["auth"] == ("ada@example.com", STORED_TOKEN)
    data = await temp_storage.get_app_password_with_scopes("Ada Lovelace")
    assert set(data["scopes"]) == {"notes.read", "notes.write"}


async def test_unconfigured_host_returns_500(temp_storage, mocker):
    """With NEXTCLOUD_HOST unset, the auth gate cannot validate and returns 500
    rather than silently allowing the request."""
    await _provision_victim(temp_storage, ["notes.read"])
    _settings_with_host(mocker, host="")

    client = TestClient(_app(temp_storage))
    resp = client.get(
        "/api/v1/users/victim/access",
        headers={"Authorization": _basic_auth("victim", STORED_TOKEN)},
    )
    assert resp.status_code == 500
    assert resp.json()["error"] == "Server not configured"
