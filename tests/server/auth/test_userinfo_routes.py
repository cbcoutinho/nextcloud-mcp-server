"""Unit tests for user info routes.

Note: Most unit tests were removed as they relied on the old _get_user_info API.
The new browser OAuth session-based implementation is covered by integration tests
in tests/server/oauth/test_userinfo_integration.py which test the full OAuth flow
with real browser sessions, token storage, and IdP interactions.

These unit tests cover only the simple _query_idp_userinfo helper function.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from nextcloud_mcp_server.auth.userinfo_routes import _query_idp_userinfo

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_query_idp_userinfo_success(mocker):
    """Test successful IdP userinfo query."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "sub": "alice",
        "email": "alice@example.com",
        "name": "Alice Smith",
    }
    mock_response.raise_for_status = Mock()

    # Mock the async context manager properly
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    mocker.patch(
        "nextcloud_mcp_server.auth.userinfo_routes.httpx.AsyncClient",
        return_value=mock_client,
    )

    result = await _query_idp_userinfo("test_token", "https://example.com/userinfo")

    assert result == {
        "sub": "alice",
        "email": "alice@example.com",
        "name": "Alice Smith",
    }
    mock_client.get.assert_called_once_with(
        "https://example.com/userinfo",
        headers={"Authorization": "Bearer test_token"},
    )


@pytest.mark.asyncio
async def test_query_idp_userinfo_failure(mocker):
    """Test IdP userinfo query failure handling."""
    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("Network error")
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    mocker.patch(
        "nextcloud_mcp_server.auth.userinfo_routes.httpx.AsyncClient",
        return_value=mock_client,
    )

    result = await _query_idp_userinfo("test_token", "https://example.com/userinfo")

    assert result is None
