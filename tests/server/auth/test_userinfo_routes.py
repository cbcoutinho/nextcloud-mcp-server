"""Unit tests for user info routes."""

from unittest.mock import AsyncMock, Mock

import pytest

from nextcloud_mcp_server.auth.userinfo_routes import (
    _query_idp_userinfo,
    user_info_html,
    user_info_json,
)

pytestmark = pytest.mark.unit

# TODO: These tests need updating to match new _get_user_info API
# which takes a Request object instead of separate parameters.
# The function was refactored to use the Starlette request object directly.


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

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response

    mocker.patch("httpx.AsyncClient", return_value=mock_client)

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

    mocker.patch("httpx.AsyncClient", return_value=mock_client)

    result = await _query_idp_userinfo("test_token", "https://example.com/userinfo")

    assert result is None


@pytest.mark.skip(
    reason="Old API tests - _get_user_info now requires full Request object with browser session. "
    "Browser OAuth flow is covered by integration tests in test_userinfo_integration.py"
)
@pytest.mark.asyncio
async def test_get_user_context_basic_auth(monkeypatch):
    """Test get_user_context in BasicAuth mode."""
    pass


@pytest.mark.skip(
    reason="Old API tests - _get_user_info now requires full Request object with browser session. "
    "Browser OAuth flow is covered by integration tests in test_userinfo_integration.py"
)
@pytest.mark.asyncio
async def test_get_user_context_oauth_no_token():
    """Test get_user_context in OAuth mode without token."""
    pass


@pytest.mark.skip(
    reason="Old API tests - _get_user_info now requires full Request object with browser session. "
    "Browser OAuth flow is covered by integration tests in test_userinfo_integration.py"
)
@pytest.mark.asyncio
async def test_get_user_context_oauth_with_token_no_idp_query(mocker):
    """Test get_user_context in OAuth mode with token but no IdP query."""
    pass


@pytest.mark.skip(
    reason="Old API tests - _get_user_info now requires full Request object with browser session. "
    "Browser OAuth flow is covered by integration tests in test_userinfo_integration.py"
)
@pytest.mark.asyncio
async def test_get_user_context_oauth_with_idp_query_success(mocker):
    """Test get_user_context in OAuth mode with successful IdP query."""
    pass


@pytest.mark.skip(
    reason="Old API tests - _get_user_info now requires full Request object with browser session. "
    "Browser OAuth flow is covered by integration tests in test_userinfo_integration.py"
)
@pytest.mark.asyncio
async def test_get_user_context_oauth_with_idp_query_failure(mocker):
    """Test get_user_context in OAuth mode with failed IdP query."""
    pass


@pytest.mark.asyncio
async def test_user_info_json_basic_auth(mocker, monkeypatch):
    """Test user_info_json endpoint in BasicAuth mode."""
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "admin")
    monkeypatch.setenv("NEXTCLOUD_HOST", "https://cloud.example.com")

    mock_request = Mock()
    mock_request.app = Mock()
    mock_request.app.state = Mock()
    mock_request.app.state.oauth_context = None

    response = await user_info_json(mock_request)

    assert response.status_code == 200
    body = response.body.decode()
    assert "admin" in body
    assert "basic" in body


@pytest.mark.asyncio
async def test_user_info_json_oauth_unauthenticated(mocker):
    """Test user_info_json endpoint in OAuth mode without authentication."""
    mock_request = Mock()
    mock_request.app = Mock()
    mock_request.app.state = Mock()
    mock_request.app.state.oauth_context = {"token_verifier": Mock()}
    mock_request.user = Mock(spec=[])  # No access_token

    response = await user_info_json(mock_request)

    assert response.status_code == 401
    body = response.body.decode()
    assert "error" in body


@pytest.mark.asyncio
async def test_user_info_json_oauth_authenticated(mocker):
    """Test user_info_json endpoint in OAuth mode with authentication."""
    mock_access_token = Mock()
    mock_access_token.resource = "alice"
    mock_access_token.client_id = "mcp_client_123"
    mock_access_token.scopes = ["notes:read", "calendar:write"]
    mock_access_token.expires_at = 1730678400
    mock_access_token.token = "test_token"

    mock_request = Mock()
    mock_request.app = Mock()
    mock_request.app.state = Mock()
    mock_request.app.state.oauth_context = {"token_verifier": Mock()}
    mock_request.user = Mock()
    mock_request.user.access_token = mock_access_token

    response = await user_info_json(mock_request)

    assert response.status_code == 200
    body = response.body.decode()
    assert "alice" in body
    assert "oauth" in body
    assert "mcp_client_123" in body


@pytest.mark.asyncio
async def test_user_info_html_basic_auth(mocker, monkeypatch):
    """Test user_info_html endpoint in BasicAuth mode."""
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "admin")
    monkeypatch.setenv("NEXTCLOUD_HOST", "https://cloud.example.com")

    mock_request = Mock()
    mock_request.app = Mock()
    mock_request.app.state = Mock()
    mock_request.app.state.oauth_context = None

    response = await user_info_html(mock_request)

    assert response.status_code == 200
    body = response.body.decode()
    assert "<!DOCTYPE html>" in body
    assert "admin" in body
    assert "basic" in body.lower()


@pytest.mark.asyncio
async def test_user_info_html_oauth_unauthenticated(mocker):
    """Test user_info_html endpoint in OAuth mode without authentication."""
    mock_request = Mock()
    mock_request.app = Mock()
    mock_request.app.state = Mock()
    mock_request.app.state.oauth_context = {"token_verifier": Mock()}
    mock_request.user = Mock(spec=[])  # No access_token

    response = await user_info_html(mock_request)

    assert response.status_code == 401
    body = response.body.decode()
    assert "<!DOCTYPE html>" in body
    assert "Authentication Required" in body


@pytest.mark.asyncio
async def test_user_info_html_oauth_authenticated(mocker):
    """Test user_info_html endpoint in OAuth mode with authentication."""
    mock_access_token = Mock()
    mock_access_token.resource = "bob"
    mock_access_token.client_id = "mcp_client_456"
    mock_access_token.scopes = ["notes:write"]
    mock_access_token.expires_at = 1730678400
    mock_access_token.token = "test_token"

    mock_request = Mock()
    mock_request.app = Mock()
    mock_request.app.state = Mock()
    mock_request.app.state.oauth_context = {"token_verifier": Mock()}
    mock_request.user = Mock()
    mock_request.user.access_token = mock_access_token

    response = await user_info_html(mock_request)

    assert response.status_code == 200
    body = response.body.decode()
    assert "<!DOCTYPE html>" in body
    assert "bob" in body
    assert "oauth" in body.lower()
    assert "mcp_client_456" in body


@pytest.mark.asyncio
async def test_user_info_html_with_scopes(mocker):
    """Test user_info_html displays scopes correctly."""
    mock_access_token = Mock()
    mock_access_token.resource = "charlie"
    mock_access_token.client_id = "mcp_client_789"
    mock_access_token.scopes = ["notes:read", "notes:write", "calendar:read"]
    mock_access_token.expires_at = 1730678400
    mock_access_token.token = "test_token"

    mock_request = Mock()
    mock_request.app = Mock()
    mock_request.app.state = Mock()
    mock_request.app.state.oauth_context = {"token_verifier": Mock()}
    mock_request.user = Mock()
    mock_request.user.access_token = mock_access_token

    response = await user_info_html(mock_request)

    assert response.status_code == 200
    body = response.body.decode()
    assert "notes:read" in body
    assert "notes:write" in body
    assert "calendar:read" in body
    assert "<h2>Scopes</h2>" in body
