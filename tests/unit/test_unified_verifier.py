"""
Unit tests for UnifiedTokenVerifier (ADR-005).

Tests token audience validation for both multi-audience and token exchange modes
without requiring real network calls or IdP connections.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from nextcloud_mcp_server.auth.unified_verifier import UnifiedTokenVerifier
from nextcloud_mcp_server.config import Settings

pytestmark = pytest.mark.unit


@pytest.fixture
def base_settings():
    """Create base settings for testing."""
    return Settings(
        oidc_client_id="test-client-id",
        oidc_client_secret="test-client-secret",
        oidc_issuer="https://idp.example.com",
        nextcloud_host="https://nextcloud.example.com",
        nextcloud_mcp_server_url="http://localhost:8000",
        nextcloud_resource_uri="http://localhost:8080",
        jwks_uri="https://idp.example.com/jwks",
        introspection_uri="https://idp.example.com/introspect",
        enable_token_exchange=False,  # Multi-audience mode
        token_exchange_cache_ttl=300,
    )


@pytest.fixture
def exchange_settings(base_settings):
    """Create settings for token exchange mode."""
    base_settings.enable_token_exchange = True
    return base_settings


class TestUnifiedTokenVerifierInit:
    """Test UnifiedTokenVerifier initialization."""

    def test_init_multi_audience_mode(self, base_settings):
        """Test verifier initialization in multi-audience mode."""
        verifier = UnifiedTokenVerifier(base_settings)
        assert verifier.mode == "multi-audience"
        assert verifier.settings == base_settings

    def test_init_exchange_mode(self, exchange_settings):
        """Test verifier initialization in token exchange mode."""
        verifier = UnifiedTokenVerifier(exchange_settings)
        assert verifier.mode == "exchange"
        assert verifier.settings == exchange_settings


class TestAudienceValidation:
    """Test audience validation logic."""

    def test_validate_multi_audience_both_present(self, base_settings):
        """Test MCP audience validation with both audiences present."""
        verifier = UnifiedTokenVerifier(base_settings)
        payload = {
            "aud": ["test-client-id", "http://localhost:8080"],
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }

        assert verifier._has_mcp_audience(payload) is True

    def test_validate_multi_audience_server_url_and_resource(self, base_settings):
        """Test MCP audience validation with server URL instead of client ID."""
        verifier = UnifiedTokenVerifier(base_settings)
        payload = {
            "aud": ["http://localhost:8000", "http://localhost:8080"],
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }

        assert verifier._has_mcp_audience(payload) is True

    def test_validate_multi_audience_missing_mcp(self, base_settings):
        """Test MCP audience validation fails without MCP audience."""
        verifier = UnifiedTokenVerifier(base_settings)
        payload = {
            "aud": ["http://localhost:8080"],  # Only Nextcloud
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }

        assert verifier._has_mcp_audience(payload) is False

    def test_validate_multi_audience_missing_nextcloud(self, base_settings):
        """Test MCP audience validation succeeds with only MCP audience (RFC 7519 compliant)."""
        verifier = UnifiedTokenVerifier(base_settings)
        payload = {
            "aud": ["test-client-id"],  # Only MCP
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }

        # Per RFC 7519, we only validate MCP audience. Nextcloud validates its own.
        assert verifier._has_mcp_audience(payload) is True

    def test_validate_multi_audience_string_audience(self, base_settings):
        """Test MCP audience validation with string audience works (RFC 7519 compliant)."""
        verifier = UnifiedTokenVerifier(base_settings)
        payload = {
            "aud": "test-client-id",  # Single audience as string
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }

        # Should pass - we only validate MCP audience per RFC 7519
        assert verifier._has_mcp_audience(payload) is True

    def test_has_mcp_audience_with_client_id(self, exchange_settings):
        """Test MCP audience validation with client ID."""
        verifier = UnifiedTokenVerifier(exchange_settings)
        payload = {
            "aud": ["test-client-id"],
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }

        assert verifier._has_mcp_audience(payload) is True

    def test_has_mcp_audience_with_server_url(self, exchange_settings):
        """Test MCP audience validation with server URL."""
        verifier = UnifiedTokenVerifier(exchange_settings)
        payload = {
            "aud": ["http://localhost:8000"],
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }

        assert verifier._has_mcp_audience(payload) is True

    def test_has_mcp_audience_missing(self, exchange_settings):
        """Test MCP audience validation fails without MCP audience."""
        verifier = UnifiedTokenVerifier(exchange_settings)
        payload = {
            "aud": ["http://localhost:8080"],  # Wrong audience
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }

        assert verifier._has_mcp_audience(payload) is False


class TestTokenFormatDetection:
    """Test JWT format detection."""

    def test_is_jwt_format_valid(self, base_settings):
        """Test JWT format detection with valid JWT."""
        verifier = UnifiedTokenVerifier(base_settings)
        jwt_token = "eyJhbGc.eyJzdWI.signature"
        assert verifier._is_jwt_format(jwt_token) is True

    def test_is_jwt_format_opaque(self, base_settings):
        """Test JWT format detection with opaque token."""
        verifier = UnifiedTokenVerifier(base_settings)
        opaque_token = "opaque-token-12345"
        assert verifier._is_jwt_format(opaque_token) is False


class TestTokenCaching:
    """Test token caching functionality."""

    async def test_cache_stores_and_retrieves(self, base_settings):
        """Test token caching stores and retrieves tokens."""
        verifier = UnifiedTokenVerifier(base_settings)

        # Create a valid access token
        payload = {
            "aud": ["test-client-id", "http://localhost:8080"],
            "sub": "testuser",
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
            "client_id": "test-client-id",
        }
        test_token = jwt.encode(payload, "secret", algorithm="HS256")

        # Create AccessToken and cache it
        access_token = verifier._create_access_token(test_token, payload)
        assert access_token is not None

        # Should retrieve from cache
        cached = verifier._get_cached_token(test_token)
        assert cached is not None
        assert cached.resource == "testuser"
        assert cached.scopes == ["openid", "profile"]

    async def test_cache_respects_expiry(self, base_settings):
        """Test that expired tokens are not returned from cache."""
        verifier = UnifiedTokenVerifier(base_settings)

        # Create expired token payload
        payload = {
            "aud": ["test-client-id", "http://localhost:8080"],
            "sub": "testuser",
            "scope": "openid profile",
            "exp": int(time.time() - 100),  # Expired 100 seconds ago
            "client_id": "test-client-id",
        }
        test_token = jwt.encode(payload, "secret", algorithm="HS256")

        # Create and cache
        access_token = verifier._create_access_token(test_token, payload)
        assert access_token is not None

        # Should not retrieve expired token
        cached = verifier._get_cached_token(test_token)
        assert cached is None

    async def test_cache_clear(self, base_settings):
        """Test cache clearing."""
        verifier = UnifiedTokenVerifier(base_settings)

        # Create and cache token
        payload = {
            "aud": ["test-client-id", "http://localhost:8080"],
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }
        test_token = jwt.encode(payload, "secret", algorithm="HS256")
        verifier._create_access_token(test_token, payload)

        # Clear cache
        verifier.clear_cache()

        # Should not retrieve after clear
        cached = verifier._get_cached_token(test_token)
        assert cached is None


class TestMultiAudienceVerification:
    """Test multi-audience token verification."""

    async def test_verify_multi_audience_with_introspection(self, base_settings):
        """Test multi-audience verification using introspection."""
        verifier = UnifiedTokenVerifier(base_settings)

        # Mock introspection response
        introspection_response = {
            "active": True,
            "sub": "testuser",
            "aud": ["test-client-id", "http://localhost:8080"],
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
            "client_id": "test-client-id",
        }

        with patch.object(
            verifier, "_introspect_token", return_value=introspection_response
        ):
            opaque_token = "opaque-token-12345"
            result = await verifier._verify_mcp_audience(opaque_token)

            assert result is not None
            assert result.resource == "testuser"
            assert result.scopes == ["openid", "profile"]

    async def test_verify_multi_audience_fails_without_both_audiences(
        self, base_settings
    ):
        """Test MCP audience verification succeeds with only MCP audience (RFC 7519 compliant)."""
        verifier = UnifiedTokenVerifier(base_settings)

        # Mock introspection response with only MCP audience
        introspection_response = {
            "active": True,
            "sub": "testuser",
            "aud": [
                "test-client-id"
            ],  # Only MCP audience (Nextcloud validates its own)
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
        }

        with patch.object(
            verifier, "_introspect_token", return_value=introspection_response
        ):
            opaque_token = "opaque-token-12345"
            result = await verifier._verify_mcp_audience(opaque_token)

            # Should succeed with only MCP audience per RFC 7519
            assert result is not None
            assert result.resource == "testuser"


class TestExchangeModeVerification:
    """Test token exchange mode verification."""

    async def test_verify_mcp_audience_only_success(self, exchange_settings):
        """Test MCP-only audience verification succeeds with MCP audience."""
        verifier = UnifiedTokenVerifier(exchange_settings)

        # Mock introspection response with MCP audience only
        introspection_response = {
            "active": True,
            "sub": "testuser",
            "aud": ["test-client-id"],
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
            "client_id": "test-client-id",
        }

        with patch.object(
            verifier, "_introspect_token", return_value=introspection_response
        ):
            opaque_token = "opaque-token-12345"
            result = await verifier._verify_mcp_audience(opaque_token)

            assert result is not None
            assert result.resource == "testuser"

    async def test_verify_mcp_audience_only_fails_without_mcp(self, exchange_settings):
        """Test MCP audience verification fails without MCP audience."""
        verifier = UnifiedTokenVerifier(exchange_settings)

        # Mock introspection response without MCP audience
        introspection_response = {
            "active": True,
            "sub": "testuser",
            "aud": ["http://localhost:8080"],  # Wrong audience
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
        }

        with patch.object(
            verifier, "_introspect_token", return_value=introspection_response
        ):
            opaque_token = "opaque-token-12345"
            result = await verifier._verify_mcp_audience(opaque_token)

            assert result is None


class TestIntrospection:
    """Test token introspection."""

    async def test_introspect_active_token(self, base_settings):
        """Test introspection of active token."""
        verifier = UnifiedTokenVerifier(base_settings)

        # Mock HTTP response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "active": True,
            "sub": "testuser",
            "aud": ["test-client-id", "http://localhost:8080"],
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
            "client_id": "test-client-id",
        }

        verifier.http_client.post = AsyncMock(return_value=mock_response)

        result = await verifier._introspect_token("test-token")
        assert result is not None
        assert result["active"] is True
        assert result["sub"] == "testuser"

    async def test_introspect_inactive_token(self, base_settings):
        """Test introspection of inactive token."""
        verifier = UnifiedTokenVerifier(base_settings)

        # Mock HTTP response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"active": False}

        verifier.http_client.post = AsyncMock(return_value=mock_response)

        result = await verifier._introspect_token("test-token")
        assert result is None

    async def test_introspect_without_endpoint(self, base_settings):
        """Test introspection when endpoint not configured."""
        base_settings.introspection_uri = None
        verifier = UnifiedTokenVerifier(base_settings)

        result = await verifier._introspect_token("test-token")
        assert result is None


class TestAccessTokenCreation:
    """Test AccessToken object creation."""

    def test_create_access_token_success(self, base_settings):
        """Test successful AccessToken creation."""
        verifier = UnifiedTokenVerifier(base_settings)

        payload = {
            "sub": "testuser",
            "scope": "openid profile email",
            "exp": int(time.time() + 3600),
            "client_id": "test-client-id",
        }
        token = "test-token-123"

        result = verifier._create_access_token(token, payload)
        assert result is not None
        assert result.token == token
        assert result.resource == "testuser"
        assert result.scopes == ["openid", "profile", "email"]
        assert result.client_id == "test-client-id"

    def test_create_access_token_with_preferred_username(self, base_settings):
        """Test AccessToken creation with preferred_username fallback."""
        verifier = UnifiedTokenVerifier(base_settings)

        payload = {
            "preferred_username": "testuser",  # No 'sub' claim
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
        }
        token = "test-token-123"

        result = verifier._create_access_token(token, payload)
        assert result is not None
        assert result.resource == "testuser"

    def test_create_access_token_no_username(self, base_settings):
        """Test AccessToken creation fails without username."""
        verifier = UnifiedTokenVerifier(base_settings)

        payload = {
            # No sub or preferred_username
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
        }
        token = "test-token-123"

        result = verifier._create_access_token(token, payload)
        assert result is None

    def test_create_access_token_no_expiry(self, base_settings):
        """Test AccessToken creation uses default TTL without expiry."""
        verifier = UnifiedTokenVerifier(base_settings)

        payload = {
            "sub": "testuser",
            "scope": "openid profile",
            # No exp claim
        }
        token = "test-token-123"

        result = verifier._create_access_token(token, payload)
        assert result is not None
        # Should have set a default expiry
        assert result.expires_at > int(time.time())


class TestVerifyTokenFlow:
    """Test complete verify_token flow."""

    async def test_verify_token_from_cache(self, base_settings):
        """Test verify_token returns cached token."""
        verifier = UnifiedTokenVerifier(base_settings)

        payload = {
            "aud": ["test-client-id", "http://localhost:8080"],
            "sub": "testuser",
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
        }
        token = jwt.encode(payload, "secret", algorithm="HS256")

        # First call - should cache
        result1 = verifier._create_access_token(token, payload)
        assert result1 is not None

        # Mock _verify_mcp_audience to ensure it's not called
        with patch.object(verifier, "_verify_mcp_audience") as mock_verify:
            result2 = await verifier.verify_token(token)
            assert result2 is not None
            assert result2.resource == "testuser"
            # Should not call verification since it's cached
            mock_verify.assert_not_called()

    async def test_verify_token_multi_audience_mode(self, base_settings):
        """Test verify_token in multi-audience mode."""
        verifier = UnifiedTokenVerifier(base_settings)

        introspection_response = {
            "active": True,
            "sub": "testuser",
            "aud": ["test-client-id", "http://localhost:8080"],
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
        }

        with patch.object(
            verifier, "_introspect_token", return_value=introspection_response
        ):
            result = await verifier.verify_token("opaque-token")
            assert result is not None
            assert result.resource == "testuser"

    async def test_verify_token_exchange_mode(self, exchange_settings):
        """Test verify_token in exchange mode."""
        verifier = UnifiedTokenVerifier(exchange_settings)

        introspection_response = {
            "active": True,
            "sub": "testuser",
            "aud": ["test-client-id"],  # MCP audience only
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
        }

        with patch.object(
            verifier, "_introspect_token", return_value=introspection_response
        ):
            result = await verifier.verify_token("opaque-token")
            assert result is not None
            assert result.resource == "testuser"
