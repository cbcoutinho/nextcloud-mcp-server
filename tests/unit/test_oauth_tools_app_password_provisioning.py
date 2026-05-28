"""Unit tests for app-password-store awareness in the provisioning tools.

Login Flow v2 (nc_auth_provision_access) and the management app-password API
write the credential to this server's ``app_passwords`` store — the same store
``require_provisioning``/``get_client`` use to grant tool access. The OAuth
provisioning tools (check_provisioning_status / revoke_nextcloud_access) must
read and clear that store too, otherwise they report "not provisioned" while
tools still work, and "nothing to revoke" while the credential persists.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.server import oauth_tools
from nextcloud_mcp_server.server.oauth_tools import (
    _get_provisioning_status,
    _revoke_nextcloud_access,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def _no_astrolabe_settings(mocker):
    """Disable the astrolabe-status branch so the app_passwords store is hit."""
    mocker.patch.object(
        oauth_tools,
        "get_settings",
        return_value=SimpleNamespace(oidc_client_id=None, oidc_client_secret=None),
    )


async def test_status_reports_provisioned_for_app_password_store(
    mocker, _no_astrolabe_settings
):
    """A Login Flow v2 app password in storage => is_provisioned with the
    app_password credential type (was previously reported as not provisioned)."""
    storage = MagicMock()
    # Only truthiness + "scopes" are read by _get_provisioning_status; omit the
    # app_password value entirely (avoids a false-positive hard-coded-credential
    # finding and keeps the mock to what the code under test actually uses).
    storage.get_app_password_with_scopes = AsyncMock(
        return_value={"scopes": ["notes.read"]}
    )
    storage.get_refresh_token = AsyncMock(return_value=None)
    mocker.patch.object(
        oauth_tools, "get_shared_storage", AsyncMock(return_value=storage)
    )

    status = await _get_provisioning_status(MagicMock(), "tester")

    assert status.is_provisioned is True
    assert status.credential_type == "app_password"
    assert status.flow_type == "login_flow_v2"
    assert status.scopes == ["notes.read"]
    storage.get_refresh_token.assert_not_awaited()  # app password short-circuits


async def test_revoke_deletes_app_password(mocker, _no_astrolabe_settings):
    """Revoke must delete the app password from storage (not just refresh tokens)."""
    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(return_value={"scopes": None})
    storage.get_refresh_token = AsyncMock(return_value=None)
    storage.delete_app_password = AsyncMock(return_value=True)
    mocker.patch.object(
        oauth_tools, "get_shared_storage", AsyncMock(return_value=storage)
    )
    mocker.patch.object(oauth_tools, "invalidate_scope_cache")

    result = await _revoke_nextcloud_access(MagicMock(), "tester")

    assert result.success is True
    storage.delete_app_password.assert_awaited_once_with("tester")
    oauth_tools.invalidate_scope_cache.assert_called_once_with("tester")


async def test_revoke_noop_when_nothing_provisioned(mocker, _no_astrolabe_settings):
    """No credential of any kind => graceful no-op, no deletion attempted."""
    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(return_value=None)
    storage.get_refresh_token = AsyncMock(return_value=None)
    storage.delete_app_password = AsyncMock()
    mocker.patch.object(
        oauth_tools, "get_shared_storage", AsyncMock(return_value=storage)
    )

    result = await _revoke_nextcloud_access(MagicMock(), "tester")

    assert result.success is True
    assert "No Nextcloud access to revoke" in result.message
    storage.delete_app_password.assert_not_awaited()
