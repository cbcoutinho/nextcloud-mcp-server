"""Integration tests for app password provisioning via Astrolabe.

Tests the complete flow for multi-user BasicAuth mode:
1. User stores app password via Astrolabe API
2. MCP server retrieves it via OAuth client credentials
3. Background sync uses it to access Nextcloud (NOT OAuth refresh tokens)

These tests verify that BasicAuth and OAuth are completely separate concerns
with no fallback between them.
"""

import pytest

from nextcloud_mcp_server.auth.astrolabe_client import AstrolabeClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector.oauth_sync import (
    NotProvisionedError,
    get_user_client,
    get_user_client_basic_auth,
    get_user_client_oauth,
)


@pytest.mark.integration
async def test_astrolabe_client_initialization():
    """Test AstrolabeClient can be instantiated."""
    client = AstrolabeClient(
        nextcloud_host="http://localhost:8080",
        client_id="test-client",
        client_secret="test-secret",
    )

    assert client is not None
    assert client.nextcloud_host == "http://localhost:8080"
    assert client.client_id == "test-client"
    assert client.client_secret == "test-secret"
    assert client._token_cache is None


@pytest.mark.integration
async def test_astrolabe_client_get_access_token_requires_oidc():
    """Test that getting access token requires OIDC discovery endpoint."""
    client = AstrolabeClient(
        nextcloud_host="http://localhost:8080",
        client_id="test-client",
        client_secret="test-secret",
    )

    # This will fail without proper OIDC setup, which is expected
    # The test verifies the client follows the OAuth client credentials flow
    try:
        token = await client.get_access_token()
        # If we get here, OIDC is configured
        assert token is not None
    except Exception as e:
        # Expected if OIDC not fully configured for test client
        # 400/401/403/404 all indicate the flow is working but credentials are invalid
        assert any(code in str(e) for code in ["400", "401", "403", "404"])


@pytest.mark.integration
async def test_get_user_app_password_returns_none_for_unconfigured_user():
    """Test that get_user_app_password returns None for users without app passwords."""
    # This requires valid OAuth client credentials
    settings = get_settings()

    if not settings.oidc_client_id or not settings.oidc_client_secret:
        pytest.skip("OAuth client credentials not configured")

    client = AstrolabeClient(
        nextcloud_host=settings.nextcloud_host or "http://localhost:8080",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
    )

    # Try to get app password for a user that hasn't provisioned one
    try:
        app_password = await client.get_user_app_password("nonexistent_user")
        # Should return None for unconfigured user (404 response)
        assert app_password is None
    except Exception as e:
        # May fail with auth error if OAuth not fully configured
        assert any(code in str(e) for code in ["400", "401", "403", "404"])


@pytest.mark.integration
async def test_basic_auth_mode_uses_app_password_only(mocker):
    """Test that BasicAuth mode uses ONLY app passwords, NOT OAuth tokens.

    In multi-user BasicAuth mode, OAuth refresh tokens are NOT used.
    This is a complete separation of concerns.
    """
    # Mock settings to have client credentials
    mock_settings = mocker.MagicMock()
    mock_settings.oidc_client_id = "test-client-id"
    mock_settings.oidc_client_secret = "test-client-secret"
    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync.get_settings",
        return_value=mock_settings,
    )

    # Mock AstrolabeClient to return an app password
    mock_astrolabe = mocker.AsyncMock()
    mock_astrolabe.get_user_app_password.return_value = "test-app-password-12345"

    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync.AstrolabeClient",
        return_value=mock_astrolabe,
    )

    # Call get_user_client in BasicAuth mode
    _client = await get_user_client(
        user_id="test_user",
        token_broker=None,  # No token broker needed for BasicAuth mode
        nextcloud_host="http://localhost:8080",
        use_basic_auth=True,
    )

    # Verify app password was requested
    mock_astrolabe.get_user_app_password.assert_called_once_with("test_user")

    # Verify client was created successfully with correct username
    assert _client is not None
    assert _client.username == "test_user"


@pytest.mark.integration
async def test_basic_auth_mode_raises_error_without_app_password(mocker):
    """Test that BasicAuth mode raises NotProvisionedError if no app password.

    There is NO fallback to OAuth - if no app password, user must provision one.
    """
    # Mock settings to have client credentials
    mock_settings = mocker.MagicMock()
    mock_settings.oidc_client_id = "test-client-id"
    mock_settings.oidc_client_secret = "test-client-secret"
    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync.get_settings",
        return_value=mock_settings,
    )

    # Mock AstrolabeClient to return None (no app password)
    mock_astrolabe = mocker.AsyncMock()
    mock_astrolabe.get_user_app_password.return_value = None

    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync.AstrolabeClient",
        return_value=mock_astrolabe,
    )

    # Call get_user_client in BasicAuth mode - should raise NotProvisionedError
    with pytest.raises(NotProvisionedError) as exc_info:
        await get_user_client(
            user_id="test_user",
            token_broker=None,
            nextcloud_host="http://localhost:8080",
            use_basic_auth=True,
        )

    # Verify error message mentions app password provisioning
    assert "app password" in str(exc_info.value).lower()
    assert "test_user" in str(exc_info.value)


@pytest.mark.integration
async def test_oauth_mode_uses_refresh_token_only(mocker):
    """Test that OAuth mode uses ONLY refresh tokens, NOT app passwords.

    In OAuth mode, app passwords are NOT used.
    This is a complete separation of concerns.
    """
    from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

    # Mock TokenBrokerService to return an access token
    mock_token_broker = mocker.AsyncMock(spec=TokenBrokerService)
    mock_token_broker.get_background_token.return_value = "test-access-token"

    # Call get_user_client in OAuth mode
    _client = await get_user_client(
        user_id="test_user",
        token_broker=mock_token_broker,
        nextcloud_host="http://localhost:8080",
        use_basic_auth=False,  # OAuth mode
    )

    # Verify token broker was called (NOT Astrolabe)
    mock_token_broker.get_background_token.assert_called_once()


@pytest.mark.integration
async def test_oauth_mode_raises_error_without_token(mocker):
    """Test that OAuth mode raises NotProvisionedError if no refresh token.

    There is NO fallback to app passwords - if no token, user must provision.
    """
    from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

    # Mock TokenBrokerService to return None (no token)
    mock_token_broker = mocker.AsyncMock(spec=TokenBrokerService)
    mock_token_broker.get_background_token.return_value = None

    # Call get_user_client in OAuth mode - should raise NotProvisionedError
    with pytest.raises(NotProvisionedError) as exc_info:
        await get_user_client(
            user_id="test_user",
            token_broker=mock_token_broker,
            nextcloud_host="http://localhost:8080",
            use_basic_auth=False,
        )

    # Verify error message mentions OAuth provisioning
    assert "oauth" in str(exc_info.value).lower()
    assert "test_user" in str(exc_info.value)


@pytest.mark.integration
async def test_get_user_client_basic_auth_function(mocker):
    """Test the dedicated get_user_client_basic_auth function."""
    # Mock settings to have client credentials
    mock_settings = mocker.MagicMock()
    mock_settings.oidc_client_id = "test-client-id"
    mock_settings.oidc_client_secret = "test-client-secret"
    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync.get_settings",
        return_value=mock_settings,
    )

    # Mock AstrolabeClient
    mock_astrolabe = mocker.AsyncMock()
    mock_astrolabe.get_user_app_password.return_value = "xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"

    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync.AstrolabeClient",
        return_value=mock_astrolabe,
    )

    # Call dedicated function
    client = await get_user_client_basic_auth(
        user_id="alice",
        nextcloud_host="http://localhost:8080",
    )

    assert client is not None
    assert client.username == "alice"
    mock_astrolabe.get_user_app_password.assert_called_once_with("alice")


@pytest.mark.integration
async def test_get_user_client_oauth_function(mocker):
    """Test the dedicated get_user_client_oauth function."""
    from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

    # Mock TokenBrokerService
    mock_token_broker = mocker.AsyncMock(spec=TokenBrokerService)
    mock_token_broker.get_background_token.return_value = "test-bearer-token"

    # Call dedicated function
    client = await get_user_client_oauth(
        user_id="alice",
        token_broker=mock_token_broker,
        nextcloud_host="http://localhost:8080",
    )

    assert client is not None
    assert client.username == "alice"
    mock_token_broker.get_background_token.assert_called_once()


@pytest.mark.integration
async def test_oauth_mode_requires_token_broker():
    """Test that OAuth mode requires a token broker."""
    with pytest.raises(ValueError, match="token_broker required"):
        await get_user_client(
            user_id="test_user",
            token_broker=None,  # Missing token broker
            nextcloud_host="http://localhost:8080",
            use_basic_auth=False,  # OAuth mode
        )
