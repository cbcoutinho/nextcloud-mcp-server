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


def _basic_auth(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


def _mock_nextcloud_rejects_credentials(mocker):
    """Make the OCS credential check behave as Nextcloud rejecting the password.

    OCS ``/cloud/user`` returns HTTP 401 for a bad app password. A correct
    handler routes this to a 401; the vulnerable handler never makes the call.
    """
    mocker.patch(
        "nextcloud_mcp_server.api.passwords.get_settings",
        return_value=MagicMock(
            nextcloud_host="http://localhost:8080",
            nextcloud_verify_ssl=True,
            nextcloud_ca_bundle=None,
        ),
    )
    mock_response = MagicMock()
    mock_response.status_code = 401

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    mocker.patch(
        "nextcloud_mcp_server.api.passwords.nextcloud_httpx_client",
        return_value=mock_client,
    )


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
    await temp_storage.store_app_password_with_scopes(
        user_id="victim",
        app_password="aaaaa-bbbbb-ccccc-ddddd-eeeee",
        scopes=["notes.read"],
        username="victim",
    )
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
    await temp_storage.store_app_password_with_scopes(
        user_id="victim",
        app_password="aaaaa-bbbbb-ccccc-ddddd-eeeee",
        scopes=["notes.read", "calendar.write"],
        username="victim_nc",
    )
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
    await temp_storage.store_app_password("victim", "aaaaa-bbbbb-ccccc-ddddd-eeeee")
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
    await temp_storage.store_app_password_with_scopes(
        user_id="victim",
        app_password="aaaaa-bbbbb-ccccc-ddddd-eeeee",
        scopes=["notes.read"],
        username="victim",
    )
    _mock_nextcloud_rejects_credentials(mocker)

    client = TestClient(_app(temp_storage))
    # RATE_LIMIT_MAX_ATTEMPTS failed attempts all return 401...
    for i in range(passwords.RATE_LIMIT_MAX_ATTEMPTS):
        resp = client.get(
            "/api/v1/users/victim/access",
            headers={"Authorization": _basic_auth("victim", "WRONG-guess")},
        )
        assert resp.status_code == 401, f"attempt {i + 1} should be 401"

    # ...the next is throttled with 429 + Retry-After.
    resp = client.get(
        "/api/v1/users/victim/access",
        headers={"Authorization": _basic_auth("victim", "WRONG-guess")},
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


async def test_invalid_credentials_error_wording_on_access(temp_storage, mocker):
    """The access/scope routes report "Invalid credentials", not the
    app-password-specific default, since they don't deal with app passwords."""
    await temp_storage.store_app_password_with_scopes(
        user_id="victim",
        app_password="aaaaa-bbbbb-ccccc-ddddd-eeeee",
        scopes=["notes.read"],
        username="victim",
    )
    _mock_nextcloud_rejects_credentials(mocker)

    client = TestClient(_app(temp_storage))
    resp = client.get(
        "/api/v1/users/victim/access",
        headers={"Authorization": _basic_auth("victim", "WRONG-guess")},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "Invalid credentials"
