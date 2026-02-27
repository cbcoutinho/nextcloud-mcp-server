"""Unit tests for @require_scopes with stored app passwords (Login Flow v2).

Tests the third enforcement mode in scope_authorization.py that checks
application-level scopes stored alongside app passwords.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from nextcloud_mcp_server.auth.scope_authorization import (
    _get_stored_scopes,
    _is_login_flow_mode,
)

pytestmark = pytest.mark.unit


def test_is_login_flow_mode_disabled():
    """Test that login flow mode is off by default."""
    with patch.dict(os.environ, {}, clear=True):
        assert _is_login_flow_mode() is False


def test_is_login_flow_mode_enabled():
    """Test that login flow mode is enabled when env var is set."""
    with patch.dict(os.environ, {"ENABLE_LOGIN_FLOW": "true"}):
        assert _is_login_flow_mode() is True


def test_is_login_flow_mode_case_insensitive():
    """Test case insensitivity of the env var."""
    with patch.dict(os.environ, {"ENABLE_LOGIN_FLOW": "True"}):
        assert _is_login_flow_mode() is True
    with patch.dict(os.environ, {"ENABLE_LOGIN_FLOW": "TRUE"}):
        assert _is_login_flow_mode() is True
    with patch.dict(os.environ, {"ENABLE_LOGIN_FLOW": "false"}):
        assert _is_login_flow_mode() is False


async def test_get_stored_scopes_with_scopes():
    """Test getting specific scopes from storage."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = {
        "app_password": "xxxxx",
        "scopes": ["notes:read", "calendar:read"],
        "username": "alice",
        "created_at": 1000,
        "updated_at": 1000,
    }
    mock_storage.initialize = AsyncMock()

    with patch(
        "nextcloud_mcp_server.auth.storage.RefreshTokenStorage.from_env",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("alice")

    assert result == ["notes:read", "calendar:read"]


async def test_get_stored_scopes_null_scopes():
    """Test that NULL scopes returns 'all'."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = {
        "app_password": "xxxxx",
        "scopes": None,
        "username": "bob",
        "created_at": 1000,
        "updated_at": 1000,
    }
    mock_storage.initialize = AsyncMock()

    with patch(
        "nextcloud_mcp_server.auth.storage.RefreshTokenStorage.from_env",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("bob")

    assert result == "all"


async def test_get_stored_scopes_no_password():
    """Test that missing app password returns None."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = None
    mock_storage.initialize = AsyncMock()

    with patch(
        "nextcloud_mcp_server.auth.storage.RefreshTokenStorage.from_env",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("nobody")

    assert result is None


async def test_get_stored_scopes_storage_error():
    """Test that storage errors return None (fail-closed)."""
    mock_storage = AsyncMock()
    mock_storage.initialize.side_effect = RuntimeError("DB error")

    with patch(
        "nextcloud_mcp_server.auth.storage.RefreshTokenStorage.from_env",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("alice")

    assert result is None
