"""
Unit tests for UnifiedTokenVerifier (ADR-005).

Tests multi-audience token validation without requiring real network calls or
IdP connections.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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
    )


class TestUnifiedTokenVerifierInit:
    """Test UnifiedTokenVerifier initialization."""

    def test_init(self, base_settings):
        """Test verifier initialization (multi-audience only; no token exchange)."""
        verifier = UnifiedTokenVerifier(base_settings)
        assert verifier.settings == base_settings


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

    def test_has_mcp_audience_with_client_id(self, base_settings):
        """Test MCP audience validation with client ID."""
        verifier = UnifiedTokenVerifier(base_settings)
        payload = {
            "aud": ["test-client-id"],
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }

        assert verifier._has_mcp_audience(payload) is True

    def test_has_mcp_audience_with_server_url(self, base_settings):
        """Test MCP audience validation with server URL."""
        verifier = UnifiedTokenVerifier(base_settings)
        payload = {
            "aud": ["http://localhost:8000"],
            "sub": "testuser",
            "exp": int(time.time() + 3600),
        }

        assert verifier._has_mcp_audience(payload) is True

    def test_has_mcp_audience_missing(self, base_settings):
        """Test MCP audience validation fails without MCP audience."""
        verifier = UnifiedTokenVerifier(base_settings)
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


class TestMcpAudienceVerification:
    """Test MCP audience verification."""

    async def test_verify_mcp_audience_only_success(self, base_settings):
        """Test MCP-only audience verification succeeds with MCP audience."""
        verifier = UnifiedTokenVerifier(base_settings)

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

    async def test_verify_mcp_audience_only_fails_without_mcp(self, base_settings):
        """Test MCP audience verification fails without MCP audience."""
        verifier = UnifiedTokenVerifier(base_settings)

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

    async def test_verify_token_mcp_audience_only(self, base_settings):
        """Test verify_token with MCP audience only."""
        verifier = UnifiedTokenVerifier(base_settings)

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


class TestManagementApiAllowlist:
    """Test ALLOWED_MGMT_CLIENT enforcement in verify_token_for_management_api."""

    @staticmethod
    def _underlying_token(client_id: str = "astrolabe"):
        from mcp.server.auth.provider import AccessToken

        return AccessToken(
            token="t",
            client_id=client_id,
            scopes=["openid"],
            expires_at=int(time.time() + 3600),
            resource="testuser",
        )

    async def test_unset_allowlist_rejects_all(self, monkeypatch, base_settings):
        monkeypatch.delenv("ALLOWED_MGMT_CLIENT", raising=False)
        verifier = UnifiedTokenVerifier(base_settings)
        assert verifier._allowed_mgmt_clients == frozenset()

        with patch.object(
            verifier,
            "_verify_without_audience_check",
            return_value=self._underlying_token("astrolabe"),
        ):
            result = await verifier.verify_token_for_management_api("any-token")
            assert result is None

    async def test_empty_allowlist_rejects_all(self, monkeypatch, base_settings):
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "  , ,")
        verifier = UnifiedTokenVerifier(base_settings)
        assert verifier._allowed_mgmt_clients == frozenset()

        with patch.object(
            verifier,
            "_verify_without_audience_check",
            return_value=self._underlying_token("astrolabe"),
        ):
            result = await verifier.verify_token_for_management_api("any-token")
            assert result is None

    async def test_allowlisted_client_accepted(self, monkeypatch, base_settings):
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe, admin-tool")
        # refresh dynaconf so the env mutation above is seen
        from nextcloud_mcp_server.config import _reload_config

        _reload_config()
        verifier = UnifiedTokenVerifier(base_settings)
        assert verifier._allowed_mgmt_clients == {"astrolabe", "admin-tool"}

        underlying = self._underlying_token("astrolabe")
        with patch.object(
            verifier, "_verify_without_audience_check", return_value=underlying
        ):
            result = await verifier.verify_token_for_management_api("any-token")
            assert result is underlying

    async def test_non_allowlisted_client_rejected(self, monkeypatch, base_settings):
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe")
        verifier = UnifiedTokenVerifier(base_settings)

        with patch.object(
            verifier,
            "_verify_without_audience_check",
            return_value=self._underlying_token("some-other-client"),
        ):
            result = await verifier.verify_token_for_management_api("any-token")
            assert result is None

    async def test_token_missing_client_id_rejected(self, monkeypatch, base_settings):
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe")
        verifier = UnifiedTokenVerifier(base_settings)

        with patch.object(
            verifier,
            "_verify_without_audience_check",
            return_value=self._underlying_token(""),
        ):
            result = await verifier.verify_token_for_management_api("any-token")
            assert result is None

    async def test_underlying_verification_failure_propagates(
        self, monkeypatch, base_settings
    ):
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe")
        verifier = UnifiedTokenVerifier(base_settings)

        with patch.object(
            verifier, "_verify_without_audience_check", return_value=None
        ):
            result = await verifier.verify_token_for_management_api("any-token")
            assert result is None

    async def test_cache_hit_also_enforces_allowlist(self, monkeypatch, base_settings):
        """A previously-cached token must still be re-checked against the allowlist."""
        import hashlib

        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe")
        verifier = UnifiedTokenVerifier(base_settings)

        token = "cached-token"
        cache_key = f"mgmt:{hashlib.sha256(token.encode()).hexdigest()}"
        verifier._token_cache[cache_key] = (
            {
                "sub": "testuser",
                "scope": "openid",
                "client_id": "not-allowlisted",
            },
            time.time() + 3600,
        )

        result = await verifier.verify_token_for_management_api(token)
        assert result is None


class TestUserinfoFallback:
    """Opaque tokens that introspection reports inactive fall back to userinfo.

    Covers the nx101294 case: the Astrolabe OIDC client issues opaque access
    tokens that Nextcloud's oidc app introspection reports active=false
    cross-client. The userinfo endpoint validates them regardless of client.
    """

    @pytest.fixture
    def userinfo_settings(self, base_settings):
        base_settings.userinfo_uri = "https://idp.example.com/userinfo"
        return base_settings

    async def test_validate_via_userinfo_success(self, userinfo_settings):
        verifier = UnifiedTokenVerifier(userinfo_settings)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"sub": "testuser"}
        with patch.object(
            verifier.http_client, "get", AsyncMock(return_value=mock_resp)
        ):
            result = await verifier._validate_via_userinfo("opaque-token")
        assert result is not None
        assert result["sub"] == "testuser"

    async def test_validate_via_userinfo_non_200(self, userinfo_settings):
        verifier = UnifiedTokenVerifier(userinfo_settings)
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch.object(
            verifier.http_client, "get", AsyncMock(return_value=mock_resp)
        ):
            result = await verifier._validate_via_userinfo("opaque-token")
        assert result is None

    async def test_validate_via_userinfo_missing_sub(self, userinfo_settings):
        verifier = UnifiedTokenVerifier(userinfo_settings)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"name": "no sub claim"}
        with patch.object(
            verifier.http_client, "get", AsyncMock(return_value=mock_resp)
        ):
            result = await verifier._validate_via_userinfo("opaque-token")
        assert result is None

    async def test_validate_via_userinfo_rejects_non_http_scheme(
        self, userinfo_settings
    ):
        """A non-http(s) userinfo_uri is refused before any request (SSRF guard)."""
        verifier = UnifiedTokenVerifier(userinfo_settings)
        verifier.userinfo_uri = "ftp://evil/userinfo"
        get_mock = AsyncMock()
        with patch.object(verifier.http_client, "get", get_mock):
            result = await verifier._validate_via_userinfo("opaque-token")
        assert result is None
        get_mock.assert_not_called()

    async def test_validate_via_userinfo_not_configured(self, base_settings):
        base_settings.userinfo_uri = None
        verifier = UnifiedTokenVerifier(base_settings)
        result = await verifier._validate_via_userinfo("opaque-token")
        assert result is None

    async def test_mgmt_opaque_userinfo_fallback_accepted_despite_allowlist(
        self, monkeypatch, userinfo_settings
    ):
        """Introspection inactive -> userinfo validates -> accepted even though
        no client_id matches the allowlist (per-user authorization applies)."""
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe")
        verifier = UnifiedTokenVerifier(userinfo_settings)

        with (
            patch.object(verifier, "_introspect_token", AsyncMock(return_value=None)),
            patch.object(
                verifier,
                "_validate_via_userinfo",
                AsyncMock(return_value={"sub": "testuser"}),
            ),
        ):
            result = await verifier.verify_token_for_management_api("opaque-token-123")

        assert result is not None
        assert result.resource == "testuser"
        assert result.client_id == ""  # userinfo provides no client_id
        # Contract: userinfo tokens carry empty scopes — management endpoints
        # must not gate on scopes for this path (per-user authz is the gate).
        assert result.scopes == []

    async def test_introspection_cannot_forge_userinfo_bypass(
        self, monkeypatch, userinfo_settings
    ):
        """A malicious `_auth_via_userinfo` claim in an introspection response
        must NOT bypass the allowlist — the bypass flag is derived from how we
        validated (validation_method), never from the IdP payload."""
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe")
        verifier = UnifiedTokenVerifier(userinfo_settings)

        malicious = {
            "sub": "testuser",
            "client_id": "not-allowlisted",
            "exp": int(time.time() + 3600),
            "_auth_via_userinfo": True,
        }
        with patch.object(
            verifier, "_introspect_token", AsyncMock(return_value=malicious)
        ):
            result = await verifier.verify_token_for_management_api("opaque-evil")

        assert result is None  # allowlist still enforced; forged flag ignored

    async def test_userinfo_used_when_introspection_unconfigured(
        self, monkeypatch, base_settings
    ):
        """With no introspection endpoint but a userinfo endpoint, opaque tokens
        go straight to userinfo (introspection is not even attempted)."""
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe")
        base_settings.introspection_uri = None
        base_settings.userinfo_uri = "https://idp.example.com/userinfo"
        verifier = UnifiedTokenVerifier(base_settings)
        assert verifier.introspection_uri is None

        introspect_mock = AsyncMock(return_value=None)
        with (
            patch.object(verifier, "_introspect_token", introspect_mock),
            patch.object(
                verifier,
                "_validate_via_userinfo",
                AsyncMock(return_value={"sub": "testuser"}),
            ),
        ):
            result = await verifier.verify_token_for_management_api("opaque-x")

        assert result is not None
        assert result.resource == "testuser"
        introspect_mock.assert_not_called()  # skipped when unconfigured

    async def test_introspection_timeout_falls_through_to_userinfo(
        self, monkeypatch, userinfo_settings
    ):
        """A real introspection timeout (caught inside _introspect_token, which
        returns None) falls through to userinfo — the authoritative live check —
        exercising the whole chain, not just a mocked _introspect_token."""
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe")
        verifier = UnifiedTokenVerifier(userinfo_settings)

        userinfo_resp = MagicMock()
        userinfo_resp.status_code = 200
        userinfo_resp.json.return_value = {"sub": "testuser"}
        with (
            patch.object(
                verifier.http_client,
                "post",
                AsyncMock(side_effect=httpx.TimeoutException("introspect down")),
            ),
            patch.object(
                verifier.http_client, "get", AsyncMock(return_value=userinfo_resp)
            ),
        ):
            result = await verifier.verify_token_for_management_api("opaque-timeout")

        assert result is not None
        assert result.resource == "testuser"

    async def test_mcp_path_does_not_use_userinfo_for_opaque_token(
        self, userinfo_settings
    ):
        """The userinfo fallback applies only to the management API path, never
        the MCP-audience path — an opaque token there is still rejected."""
        verifier = UnifiedTokenVerifier(userinfo_settings)
        userinfo_mock = AsyncMock(return_value={"sub": "testuser"})
        with (
            patch.object(verifier, "_introspect_token", AsyncMock(return_value=None)),
            patch.object(verifier, "_validate_via_userinfo", userinfo_mock),
        ):
            result = await verifier.verify_token("opaque-astrolabe-token")
        assert result is None
        userinfo_mock.assert_not_called()

    async def test_opaque_rejected_when_no_validators_configured(self, base_settings):
        """With neither introspection nor userinfo configured, an opaque token is
        rejected without recording a misleading userinfo-failure metric."""
        base_settings.introspection_uri = None
        base_settings.userinfo_uri = None
        verifier = UnifiedTokenVerifier(base_settings)
        assert verifier.introspection_uri is None
        assert verifier.userinfo_uri is None

        result = await verifier._verify_without_audience_check(
            "opaque-no-validator", "mgmt:none"
        )
        assert result is None

    async def test_mgmt_userinfo_not_called_when_introspection_succeeds(
        self, monkeypatch, userinfo_settings
    ):
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe")
        # refresh dynaconf so the env mutation above is seen
        from nextcloud_mcp_server.config import _reload_config

        _reload_config()
        verifier = UnifiedTokenVerifier(userinfo_settings)

        introspection_payload = {
            "sub": "testuser",
            "client_id": "astrolabe",
            "scope": "openid",
            "exp": int(time.time() + 3600),
        }
        userinfo_mock = AsyncMock(return_value={"sub": "x", "_auth_via_userinfo": True})
        with (
            patch.object(
                verifier,
                "_introspect_token",
                AsyncMock(return_value=introspection_payload),
            ),
            patch.object(verifier, "_validate_via_userinfo", userinfo_mock),
        ):
            result = await verifier.verify_token_for_management_api("opaque-token-123")

        assert result is not None
        assert result.client_id == "astrolabe"
        userinfo_mock.assert_not_called()

    async def test_mgmt_cache_hit_also_bypasses_allowlist_for_userinfo_tokens(
        self, monkeypatch, userinfo_settings
    ):
        """A second call (cache hit) with a via-userinfo token still bypasses
        the allowlist and is served from cache (no second network probe).

        Seeds the cache via a real first call rather than constructing the cache
        key by hand, so the test exercises behavior, not cache internals."""
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "astrolabe")
        verifier = UnifiedTokenVerifier(userinfo_settings)

        userinfo_mock = AsyncMock(return_value={"sub": "testuser"})
        with (
            patch.object(verifier, "_introspect_token", AsyncMock(return_value=None)),
            patch.object(verifier, "_validate_via_userinfo", userinfo_mock),
        ):
            first = await verifier.verify_token_for_management_api("opaque-cached")
            second = await verifier.verify_token_for_management_api("opaque-cached")

        assert first is not None and second is not None
        assert second.resource == "testuser"
        # Second call served from cache — userinfo probed only once.
        userinfo_mock.assert_awaited_once()

    def test_userinfo_token_cached_with_short_ttl(self, userinfo_settings):
        """userinfo tokens (no exp) get the short userinfo TTL, not the 1h default.

        The short TTL is keyed off the explicit via_userinfo argument, not a
        payload claim."""
        verifier = UnifiedTokenVerifier(userinfo_settings)
        verifier.userinfo_cache_ttl = 300

        before = time.time()
        access_token = verifier._create_access_token_with_cache_key(
            "opaque-token", {"sub": "testuser"}, "mgmt:test", via_userinfo=True
        )
        assert access_token is not None
        # Expiry should sit within the short userinfo window, well under 1h.
        assert access_token.expires_at <= int(before + 300) + 2
        assert access_token.expires_at < int(before + verifier.cache_ttl)

    def test_userinfo_token_with_exp_uses_real_expiry(self, userinfo_settings):
        """When userinfo (unusually) returns an exp, the real token expiry wins
        over the short userinfo TTL."""
        verifier = UnifiedTokenVerifier(userinfo_settings)
        verifier.userinfo_cache_ttl = 300
        real_exp = int(time.time() + 4000)  # far beyond the 300s short TTL

        access_token = verifier._create_access_token_with_cache_key(
            "opaque-token",
            {"sub": "testuser", "exp": real_exp},
            "mgmt:test-exp",
            via_userinfo=True,
        )
        assert access_token is not None
        assert access_token.expires_at == real_exp

    async def test_validate_via_userinfo_timeout(self, userinfo_settings):
        verifier = UnifiedTokenVerifier(userinfo_settings)
        with patch.object(
            verifier.http_client,
            "get",
            AsyncMock(side_effect=httpx.TimeoutException("timeout")),
        ):
            result = await verifier._validate_via_userinfo("opaque-token")
        assert result is None

    async def test_validate_via_userinfo_request_error(self, userinfo_settings):
        verifier = UnifiedTokenVerifier(userinfo_settings)
        with patch.object(
            verifier.http_client,
            "get",
            AsyncMock(side_effect=httpx.ConnectError("boom")),
        ):
            result = await verifier._validate_via_userinfo("opaque-token")
        assert result is None

    async def test_validate_via_userinfo_malformed_json(self, userinfo_settings):
        verifier = UnifiedTokenVerifier(userinfo_settings)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        with patch.object(
            verifier.http_client, "get", AsyncMock(return_value=mock_resp)
        ):
            result = await verifier._validate_via_userinfo("opaque-token")
        assert result is None
