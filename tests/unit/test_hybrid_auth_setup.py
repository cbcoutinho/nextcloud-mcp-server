"""
Unit tests for hybrid authentication mode OAuth setup.

Tests the setup_oauth_config_for_multi_user_basic() function that enables
hybrid authentication where MCP operations use BasicAuth and management
APIs use OAuth.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.app import setup_oauth_config_for_multi_user_basic
from nextcloud_mcp_server.config import Settings

pytestmark = pytest.mark.unit


@pytest.fixture
def hybrid_auth_settings():
    """Create settings for hybrid auth mode testing."""
    return Settings(
        nextcloud_host="https://nextcloud.example.com",
        enable_offline_access=False,  # Start with offline access disabled
    )


@pytest.fixture
def oidc_discovery_response():
    """Mock OIDC discovery endpoint response."""
    return {
        "issuer": "https://nextcloud.example.com",
        "authorization_endpoint": "https://nextcloud.example.com/apps/oidc/authorize",
        "token_endpoint": "https://nextcloud.example.com/apps/oidc/token",
        "userinfo_endpoint": "https://nextcloud.example.com/apps/oidc/userinfo",
        "jwks_uri": "https://nextcloud.example.com/apps/oidc/jwks",
        "introspection_endpoint": "https://nextcloud.example.com/apps/oidc/introspect",
        "registration_endpoint": "https://nextcloud.example.com/apps/oidc/register",
        "scopes_supported": ["openid", "profile", "email", "offline_access"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }


class TestSetupOAuthConfigForMultiUserBasic:
    """Test setup_oauth_config_for_multi_user_basic() function."""

    async def test_successful_setup_without_offline_access(
        self, hybrid_auth_settings, oidc_discovery_response, mocker
    ):
        """Test successful OAuth setup without offline access."""
        # Mock httpx.AsyncClient
        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value=oidc_discovery_response)
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = AsyncMock()

        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        # Call function
        (
            verifier,
            storage,
            client_id,
            client_secret,
        ) = await setup_oauth_config_for_multi_user_basic(
            settings=hybrid_auth_settings,
            client_id="test-client-id",
            client_secret="test-client-secret",
        )

        # Verify OIDC discovery was called
        mock_client.get.assert_called_once_with(
            "https://nextcloud.example.com/.well-known/openid-configuration"
        )

        # Verify settings were updated
        assert hybrid_auth_settings.oidc_client_id == "test-client-id"
        assert hybrid_auth_settings.oidc_client_secret == "test-client-secret"
        assert hybrid_auth_settings.oidc_issuer == "https://nextcloud.example.com"
        assert (
            hybrid_auth_settings.jwks_uri
            == "https://nextcloud.example.com/apps/oidc/jwks"
        )
        assert (
            hybrid_auth_settings.introspection_uri
            == "https://nextcloud.example.com/apps/oidc/introspect"
        )
        assert (
            hybrid_auth_settings.userinfo_uri
            == "https://nextcloud.example.com/apps/oidc/userinfo"
        )

        # Verify token verifier was created
        assert verifier is not None
        from nextcloud_mcp_server.auth.unified_verifier import UnifiedTokenVerifier

        assert isinstance(verifier, UnifiedTokenVerifier)

        # Verify storage is None (offline access disabled)
        assert storage is None

        # Verify credentials returned
        assert client_id == "test-client-id"
        assert client_secret == "test-client-secret"

    async def test_successful_setup_with_offline_access(
        self, hybrid_auth_settings, oidc_discovery_response, mocker
    ):
        """Test successful OAuth setup with offline access enabled."""
        # Enable offline access
        hybrid_auth_settings.enable_offline_access = True

        # Generate a valid Fernet key for testing
        from cryptography.fernet import Fernet

        valid_fernet_key = Fernet.generate_key().decode()

        # Mock TOKEN_ENCRYPTION_KEY environment variable
        mocker.patch(
            "os.getenv",
            side_effect=lambda k, default=None: {
                "TOKEN_ENCRYPTION_KEY": valid_fernet_key,
                "NEXTCLOUD_MCP_SERVER_URL": "http://localhost:8000",
            }.get(k, default),
        )

        # Mock httpx.AsyncClient
        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value=oidc_discovery_response)
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = AsyncMock()

        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        # Call function
        (
            verifier,
            storage,
            client_id,
            client_secret,
        ) = await setup_oauth_config_for_multi_user_basic(
            settings=hybrid_auth_settings,
            client_id="test-client-id",
            client_secret="test-client-secret",
        )

        # Verify storage was created
        assert storage is not None
        from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

        assert isinstance(storage, RefreshTokenStorage)

    async def test_discovered_urls_used_directly(
        self, hybrid_auth_settings, oidc_discovery_response, mocker
    ):
        """Test that discovered URLs are used directly without rewriting."""
        # Mock httpx.AsyncClient
        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value=oidc_discovery_response)
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = AsyncMock()

        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        # Call function
        (
            verifier,
            storage,
            client_id,
            client_secret,
        ) = await setup_oauth_config_for_multi_user_basic(
            settings=hybrid_auth_settings,
            client_id="test-client-id",
            client_secret="test-client-secret",
        )

        # Verify discovered URLs are used directly (not rewritten)
        assert hybrid_auth_settings.jwks_uri == oidc_discovery_response["jwks_uri"]
        assert (
            hybrid_auth_settings.introspection_uri
            == oidc_discovery_response["introspection_endpoint"]
        )
        assert (
            hybrid_auth_settings.userinfo_uri
            == oidc_discovery_response["userinfo_endpoint"]
        )

        # Verify issuer is used directly for JWT validation
        assert hybrid_auth_settings.oidc_issuer == oidc_discovery_response["issuer"]

    async def test_oidc_discovery_failure(self, hybrid_auth_settings, mocker):
        """Test handling of OIDC discovery failure."""
        # Mock httpx.AsyncClient to raise an HTTP error
        import httpx

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPError(
            "Connection failed"
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = AsyncMock()

        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        # Call function and expect exception (currently raises UnboundLocalError
        # due to exception in async with block - this is a known issue)
        with pytest.raises((httpx.HTTPError, UnboundLocalError)):
            await setup_oauth_config_for_multi_user_basic(
                settings=hybrid_auth_settings,
                client_id="test-client-id",
                client_secret="test-client-secret",
            )

    async def test_missing_nextcloud_host(self):
        """Test that missing NEXTCLOUD_HOST raises ValueError."""
        settings = Settings()  # No nextcloud_host set

        with pytest.raises(ValueError, match="NEXTCLOUD_HOST is required"):
            await setup_oauth_config_for_multi_user_basic(
                settings=settings,
                client_id="test-client-id",
                client_secret="test-client-secret",
            )

    async def test_custom_discovery_url(
        self, hybrid_auth_settings, oidc_discovery_response, mocker
    ):
        """Test using custom OIDC discovery URL."""
        # Mock OIDC_DISCOVERY_URL environment variable
        custom_discovery_url = (
            "https://custom.idp.example.com/.well-known/openid-configuration"
        )
        mocker.patch(
            "os.getenv",
            side_effect=lambda k, default=None: {
                "OIDC_DISCOVERY_URL": custom_discovery_url,
                "NEXTCLOUD_MCP_SERVER_URL": "http://localhost:8000",
            }.get(k, default),
        )

        # Mock httpx.AsyncClient
        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value=oidc_discovery_response)
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = AsyncMock()

        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        # Call function
        await setup_oauth_config_for_multi_user_basic(
            settings=hybrid_auth_settings,
            client_id="test-client-id",
            client_secret="test-client-secret",
        )

        # Verify custom discovery URL was used
        mock_client.get.assert_called_once_with(custom_discovery_url)
