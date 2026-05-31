"""Unit tests: stored-app-password client builders authenticate with the
Nextcloud loginName, not the UID.

Regression for the OIDC-provisioned-user case where the Nextcloud UID differs
from the loginName (e.g. UID "Ada Lovelace", loginName "ada@example.com").
Nextcloud authenticates app passwords against the loginName, so binding the UID
as the BasicAuth username returns HTTP 401 for every Notes/Files/Shares/CalDAV
call — which previously stopped the background sync scan loop and degraded
ACL-aware search to a self-only owner filter.

The builders must split the two identities:
- credential username (httpx + CalDAV auth) → loginName
- DAV/URL path identity (``client.username``)  → UID
"""

import tempfile
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit

_APP_PW = "aaaaa-bbbbb-ccccc-ddddd-eeeee"


def _basic_auth_header(username: str, password: str) -> str:
    return httpx.BasicAuth(username, password)._auth_header


@pytest.fixture
async def temp_storage():
    """Encrypted storage backed by a throwaway SQLite file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = RefreshTokenStorage(
            db_path=str(Path(tmpdir) / "test.db"),
            encryption_key=Fernet.generate_key().decode(),
        )
        await storage.initialize()
        yield storage


class _FakeAsyncClient:
    """Minimal stand-in for ``httpx.AsyncClient`` that records the ``auth=``
    kwarg and returns a canned status code."""

    def __init__(self, status_code: int):
        self._status_code = status_code
        self.captured_auth: httpx.Auth | None = None

    def __call__(self, *args, **kwargs):
        self.captured_auth = kwargs.get("auth")
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        return httpx.Response(self._status_code)


async def test_basic_auth_builder_splits_uid_and_loginname(mocker):
    """multi_user_basic background-sync builder: loginName authenticates,
    UID builds paths."""
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )
    from nextcloud_mcp_server.vector.oauth_sync import get_user_client_basic_auth

    storage = mocker.MagicMock()
    storage.get_app_password_with_scopes = mocker.AsyncMock(
        return_value={
            "app_password": "app-pw-1234",
            "username": "ada@example.com",  # loginName
            "scopes": None,
        }
    )

    client = await get_user_client_basic_auth(
        "Ada Lovelace",  # UID
        "https://cloud.example.org",
        storage=storage,
    )

    # Path identity → UID
    assert client.username == "Ada Lovelace"
    # httpx credential (notes/webdav/sharing) → loginName
    assert client._client.auth._auth_header == _basic_auth_header(
        "ada@example.com", "app-pw-1234"
    )
    # CalDAV credential → loginName, but calendar-home path → UID
    assert mock_dav_client.call_args.kwargs["username"] == "ada@example.com"
    assert client.calendar.username == "Ada Lovelace"


async def test_basic_auth_builder_legacy_row_falls_back_to_uid(mocker):
    """Legacy rows stored before the loginName column was populated have
    ``username = None`` → fall back to the UID for the credential (preserves
    the previous behaviour where UID == loginName)."""
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )
    from nextcloud_mcp_server.vector.oauth_sync import get_user_client_basic_auth

    storage = mocker.MagicMock()
    storage.get_app_password_with_scopes = mocker.AsyncMock(
        return_value={"app_password": "pw", "username": None, "scopes": None}
    )

    client = await get_user_client_basic_auth(
        "alice", "https://cloud.example.org", storage=storage
    )

    assert client.username == "alice"
    assert client._client.auth._auth_header == _basic_auth_header("alice", "pw")
    assert mock_dav_client.call_args.kwargs["username"] == "alice"


async def test_basic_auth_builder_unprovisioned_raises(mocker):
    """No stored app password → NotProvisionedError (unchanged contract)."""
    from nextcloud_mcp_server.vector.oauth_sync import (
        NotProvisionedError,
        get_user_client_basic_auth,
    )

    storage = mocker.MagicMock()
    storage.get_app_password_with_scopes = mocker.AsyncMock(return_value=None)

    with pytest.raises(NotProvisionedError):
        await get_user_client_basic_auth(
            "alice", "https://cloud.example.org", storage=storage
        )


async def test_login_flow_builder_splits_id_and_loginname(mocker):
    """Login Flow v2 per-request builder (MCP tool path): same split as the
    background-sync builder. ``user_id`` (the identity the system is keyed on,
    == NC UID for NC-as-OIDC-IdP) builds DAV paths; the stored loginName
    authenticates. Previously the loginName was used for the DAV path too,
    which is wrong for OIDC users (UID != loginName)."""
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )
    from nextcloud_mcp_server import context

    mocker.patch(
        "nextcloud_mcp_server.auth.token_utils.extract_user_id_from_token",
        mocker.AsyncMock(return_value="Ada Lovelace"),  # token sub == NC UID
    )
    storage = mocker.MagicMock()
    storage.get_app_password_with_scopes = mocker.AsyncMock(
        return_value={
            "app_password": "app-pw-9999",
            "username": "ada@example.com",  # loginName
            "scopes": None,
        }
    )
    mocker.patch.object(
        context, "get_shared_storage", mocker.AsyncMock(return_value=storage)
    )

    client = await context._get_client_from_login_flow(
        mocker.MagicMock(), "https://cloud.example.org"
    )

    # Path identity → user_id (== UID); credential → loginName
    assert client.username == "Ada Lovelace"
    assert client._client.auth._auth_header == _basic_auth_header(
        "ada@example.com", "app-pw-9999"
    )
    assert mock_dav_client.call_args.kwargs["username"] == "ada@example.com"


async def test_cleanup_authenticates_with_loginname_not_uid(temp_storage, mocker):
    """cleanup_invalid_app_passwords must validate with the stored loginName.

    Validating as the UID would 401 a *valid* OIDC password and wrongly delete
    it — the exact failure observed on a login_flow tenant in production.
    """
    await temp_storage.store_app_password_with_scopes(
        "Ada Lovelace", _APP_PW, username="ada@example.com"
    )
    fake = _FakeAsyncClient(status_code=200)  # valid credential
    mocker.patch("nextcloud_mcp_server.auth.storage.httpx.AsyncClient", fake)

    removed = await temp_storage.cleanup_invalid_app_passwords(
        "https://cloud.example.org"
    )

    assert removed == []  # valid password preserved
    assert fake.captured_auth._auth_header == _basic_auth_header(
        "ada@example.com", _APP_PW
    )
    assert await temp_storage.get_app_password("Ada Lovelace") == _APP_PW


async def test_cleanup_removes_genuinely_invalid_password(temp_storage, mocker):
    """A real 401 still removes the stored password (unchanged contract)."""
    await temp_storage.store_app_password_with_scopes(
        "alice", _APP_PW, username="alice"
    )
    fake = _FakeAsyncClient(status_code=401)
    mocker.patch("nextcloud_mcp_server.auth.storage.httpx.AsyncClient", fake)

    removed = await temp_storage.cleanup_invalid_app_passwords(
        "https://cloud.example.org"
    )

    assert removed == ["alice"]
    assert await temp_storage.get_app_password("alice") is None
