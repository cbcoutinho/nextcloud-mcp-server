"""Unit tests: stored-app-password client builders use Nextcloud loginName for
both authentication and DAV/API path construction.

The Nextcloud loginName (returned by Login Flow v2 and stored in
``app_data["username"]``) is authoritative for the Nextcloud username.  It must
be used for:
- credential username (httpx2 + CalDAV auth) → loginName
- DAV/URL path identity (``client.username``) → loginName

When Nextcloud itself is the OIDC IdP, loginName == OIDC sub == NC username, so
there is no behaviour change.  When an external OIDC provider (e.g. Keycloak)
uses ``preferred_username`` as the user identifier, the OIDC sub is a UUID that
has no Nextcloud counterpart: using the sub as the DAV path segment produces
HTTP 404.  Using loginName is always correct.

Background-sync builder (``get_user_client_basic_auth``) intentionally still
uses the UID for DAV paths because its app-password store is keyed on the UID
and the sync layer already knows the Nextcloud UID from the provisioning record.
"""

import tempfile
from pathlib import Path

import httpx2
import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.scope_cache import _scope_cache
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit

_APP_PW = "aaaaa-bbbbb-ccccc-ddddd-eeeee"


def _basic_auth_header(username: str, password: str) -> str:
    return httpx2.BasicAuth(username, password)._auth_header


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


#: Nextcloud's answer to credentials it does not accept (measured against
#: Nextcloud 35: HTTP 401 with this body).
_OCS_UNAUTHORISED = {
    "ocs": {
        "meta": {"status": "failure", "statuscode": 997, "message": "Unauthorised"},
        "data": [],
    }
}


class _FakeAsyncClient:
    """Minimal stand-in for ``httpx2.AsyncClient`` that records the ``auth=``
    kwarg and returns a canned status code and optional JSON body."""

    def __init__(self, status_code: int, json=None, on_get=None):
        self._status_code = status_code
        self._json = json
        self._on_get = on_get
        self.captured_auth: httpx2.Auth | None = None
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.captured_auth = kwargs.get("auth")
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        self.calls += 1
        if self._on_get is not None:
            await self._on_get()
        if self._json is None:
            return httpx2.Response(self._status_code)
        return httpx2.Response(self._status_code, json=self._json)


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
    # httpx2 credential (notes/webdav/sharing) → loginName
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


async def test_login_flow_builder_uses_login_name_for_dav_path(mocker):
    """Login Flow v2 per-request builder: loginName drives both DAV paths and
    authentication.

    Nextcloud's Login Flow v2 ``loginName`` field is authoritative for the
    Nextcloud username — it is set by Nextcloud itself during the flow.  When an
    external OIDC provider (e.g. Keycloak) uses ``preferred_username`` as the
    user identifier, the OIDC ``sub`` claim is a UUID that does NOT correspond
    to any Nextcloud username, so using it as the DAV path segment produces a
    404.  Using ``login_name`` (from ``app_data["username"]``) is always correct:
    when NC is the OIDC IdP, loginName == sub == NC username (no change); when
    an external IdP maps preferred_username, loginName is the actual NC username.
    """
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )
    from nextcloud_mcp_server import context

    mocker.patch(
        "nextcloud_mcp_server.auth.token_utils.extract_user_id_from_token",
        mocker.AsyncMock(return_value="Ada Lovelace"),  # token sub (OIDC identity)
    )
    storage = mocker.MagicMock()
    storage.get_app_password_with_scopes = mocker.AsyncMock(
        return_value={
            "app_password": "app-pw-9999",
            "username": "ada@example.com",  # loginName from Login Flow v2
            "scopes": None,
        }
    )
    mocker.patch.object(
        context, "get_shared_storage", mocker.AsyncMock(return_value=storage)
    )

    client = await context._get_client_from_login_flow(
        mocker.MagicMock(), "https://cloud.example.org"
    )

    # Both DAV path identity and credential → loginName (not OIDC sub)
    assert client.username == "ada@example.com"
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
    mocker.patch("nextcloud_mcp_server.auth.storage.httpx2.AsyncClient", fake)

    removed = await temp_storage.cleanup_invalid_app_passwords(
        "https://cloud.example.org"
    )

    assert removed == []  # valid password preserved
    assert fake.captured_auth._auth_header == _basic_auth_header(
        "ada@example.com", _APP_PW
    )
    assert await temp_storage.get_app_password("Ada Lovelace") == _APP_PW


async def test_cleanup_removes_genuinely_invalid_password(temp_storage, mocker):
    """Nextcloud's own rejection still removes the stored password."""
    await temp_storage.store_app_password_with_scopes(
        "alice", _APP_PW, username="alice"
    )
    fake = _FakeAsyncClient(status_code=401, json=_OCS_UNAUTHORISED)
    mocker.patch("nextcloud_mcp_server.auth.storage.httpx2.AsyncClient", fake)

    removed = await temp_storage.cleanup_invalid_app_passwords(
        "https://cloud.example.org"
    )

    assert removed == ["alice"]
    assert await temp_storage.get_app_password("alice") is None


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (200, None),
        (429, None),
        (500, None),
        # A reverse proxy, WAF or geo-block in front of Nextcloud
        (401, None),
        (403, None),
        (401, {"error": "unauthorized"}),
        # Nextcloud answering 403 with its own envelope is not a bad password
        (403, _OCS_UNAUTHORISED),
    ],
)
async def test_validation_keeps_password_unless_nextcloud_rejects_it(
    temp_storage, mocker, status_code, body
):
    """Only HTTP 401 with OCS status 997 is a rejection."""
    await temp_storage.store_app_password_with_scopes(
        "alice", _APP_PW, username="alice"
    )
    fake = _FakeAsyncClient(status_code=status_code, json=body)
    mocker.patch("nextcloud_mcp_server.auth.storage.httpx2.AsyncClient", fake)

    removed = await temp_storage.remove_app_password_if_rejected(
        "alice", "https://cloud.example.org"
    )

    assert removed is False
    assert await temp_storage.get_app_password("alice") == _APP_PW


async def test_validation_keeps_password_on_network_error(temp_storage, mocker):
    await temp_storage.store_app_password_with_scopes(
        "alice", _APP_PW, username="alice"
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.storage.nextcloud_httpx_client",
        side_effect=OSError("connection refused"),
    )

    removed = await temp_storage.remove_app_password_if_rejected(
        "alice", "https://cloud.example.org"
    )

    assert removed is False
    assert await temp_storage.get_app_password("alice") == _APP_PW


async def test_validation_skips_rows_without_loginname(temp_storage, mocker):
    """Probing with the UID would reject a working password whose loginName
    differs (#980), so a legacy row is not validated at all."""
    await temp_storage.store_app_password("Ada Lovelace", _APP_PW)
    fake = _FakeAsyncClient(status_code=401, json=_OCS_UNAUTHORISED)
    mocker.patch("nextcloud_mcp_server.auth.storage.httpx2.AsyncClient", fake)

    removed = await temp_storage.cleanup_invalid_app_passwords(
        "https://cloud.example.org"
    )

    assert removed == []
    assert fake.calls == 0
    assert await temp_storage.get_app_password("Ada Lovelace") == _APP_PW


async def test_validation_keeps_password_stored_while_checking(temp_storage, mocker):
    """A Login Flow completing while the old password is probed stores a new
    one; the rejection of the old one must not delete it."""
    await temp_storage.store_app_password_with_scopes(
        "alice", _APP_PW, username="alice"
    )

    async def reprovision():
        await temp_storage.store_app_password_with_scopes(
            "alice", "fresh-fresh-fresh-fresh-fresh", username="alice"
        )

    fake = _FakeAsyncClient(status_code=401, json=_OCS_UNAUTHORISED, on_get=reprovision)
    mocker.patch("nextcloud_mcp_server.auth.storage.httpx2.AsyncClient", fake)

    removed = await temp_storage.remove_app_password_if_rejected(
        "alice", "https://cloud.example.org"
    )

    assert removed is False
    assert (
        await temp_storage.get_app_password("alice") == "fresh-fresh-fresh-fresh-fresh"
    )


async def test_check_app_password_changes_nothing(temp_storage, mocker):
    await temp_storage.store_app_password_with_scopes(
        "alice", _APP_PW, username="alice"
    )
    fake = _FakeAsyncClient(status_code=401, json=_OCS_UNAUTHORISED)
    mocker.patch("nextcloud_mcp_server.auth.storage.httpx2.AsyncClient", fake)
    app_data = await temp_storage.get_app_password_with_scopes("alice")

    check = await temp_storage.check_app_password("https://cloud.example.org", app_data)

    assert check == "rejected"
    assert await temp_storage.get_app_password("alice") == _APP_PW


async def test_deleting_a_password_clears_cached_scopes(temp_storage):
    """The background sweep deletes through the same path, so cached scopes
    never outlive the grant."""
    await temp_storage.store_app_password_with_scopes(
        "alice", _APP_PW, username="alice"
    )
    _scope_cache["alice"] = (float("inf"), "all")

    await temp_storage.delete_app_password("alice")

    assert "alice" not in _scope_cache
