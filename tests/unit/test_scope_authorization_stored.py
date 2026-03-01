"""Unit tests for @require_scopes with stored app passwords (Login Flow v2).

Tests the third enforcement mode in scope_authorization.py that checks
application-level scopes stored alongside app passwords.
"""

from unittest.mock import AsyncMock, patch

import pytest

from nextcloud_mcp_server.auth.scope_authorization import (
    _get_stored_scopes,
)

pytestmark = pytest.mark.unit


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

    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
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

    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("bob")

    assert result == "all"


async def test_get_stored_scopes_no_password():
    """Test that missing app password returns None."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = None

    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("nobody")

    assert result is None


async def test_get_stored_scopes_storage_error():
    """Test that storage errors propagate to the caller."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.side_effect = RuntimeError("DB error")

    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
            return_value=mock_storage,
        ),
        pytest.raises(RuntimeError, match="DB error"),
    ):
        await _get_stored_scopes("alice")
