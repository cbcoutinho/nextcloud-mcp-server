"""
Unit tests for UnifiedTokenVerifier (ADR-005).

Tests multi-audience token validation without requiring real network calls or
IdP connections.
"""

import hashlib
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from mcp.server.auth.provider import AccessToken

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

    async def test_introspect_without_client_credentials(self, base_settings):
        """Missing client credentials return None without an HTTP call.

        Guards against a revert to the `assert client_id is not None` that used
        to sit inside the try block, where the AssertionError was swallowed by
        `except Exception` and logged as a failed introspection (python:S5779).
        """
        base_settings.oidc_client_secret = None
        verifier = UnifiedTokenVerifier(base_settings)
        verifier.http_client.post = AsyncMock()

        result = await verifier._introspect_token("test-token")

        assert result is None
        verifier.http_client.post.assert_not_called()

    async def test_verify_jwt_signature_without_jwks_client(self, base_settings):
        """No JWKS client returns None rather than raising into the catch-all.

        Same guard-before-try shape as the introspection case above.
        """
        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = None

        result = await verifier._verify_jwt_signature("test-token")

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

    def test_create_access_token_reads_azp_when_no_client_id(self, base_settings):
        """Keycloak et al stamp `azp` and never `client_id`.

        Reading only `client_id` left every external-IdP token looking
        client-less, so ALLOWED_MGMT_CLIENT rejected it however it was
        configured (cbcoutinho/astrolabe#324).
        """
        verifier = UnifiedTokenVerifier(base_settings)

        payload = {
            "sub": "testuser",
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
            "azp": "nextcloud",  # no client_id claim at all
        }

        result = verifier._create_access_token("test-token-123", payload)
        assert result is not None
        assert result.client_id == "nextcloud"

    def test_create_access_token_prefers_client_id_over_azp(self, base_settings):
        """RFC 9068's spelling wins when a token carries both."""
        verifier = UnifiedTokenVerifier(base_settings)

        payload = {
            "sub": "testuser",
            "scope": "openid",
            "exp": int(time.time() + 3600),
            "client_id": "rfc9068-client",
            "azp": "oidc-core-client",
        }

        result = verifier._create_access_token("test-token-123", payload)
        assert result is not None
        assert result.client_id == "rfc9068-client"

    def test_client_id_from_claims_ignores_audience(self, base_settings):
        """`aud` says who a token is for, not who obtained it.

        Accepting it as an identity would let any token minted for an
        allowlisted audience through the allowlist.
        """
        verifier = UnifiedTokenVerifier(base_settings)

        assert verifier._client_id_from_claims({"aud": "some-allowlisted-id"}) == ""
        assert verifier._client_id_from_claims({}) == ""
        assert verifier._client_id_from_claims({"azp": ""}) == ""

    def test_client_id_from_claims_falls_through_empty_client_id(self, base_settings):
        """A present-but-empty `client_id` must not shadow a usable `azp`.

        The guard tests the value, not the key, so an IdP that stamps
        `client_id: ""` alongside a real `azp` still resolves to a client
        rather than fail-closing to "".
        """
        verifier = UnifiedTokenVerifier(base_settings)

        assert (
            verifier._client_id_from_claims({"client_id": "", "azp": "nextcloud"})
            == "nextcloud"
        )
        # Non-string claims fall through the same way.
        assert (
            verifier._client_id_from_claims({"client_id": 42, "azp": "nextcloud"})
            == "nextcloud"
        )


class TestCanonicalUidResolution:
    """Regression tests for #1326.

    An external IdP's `sub` need not equal the Nextcloud UID `user_oidc`
    assigns it. `_resolve_canonical_uid` looks up the real UID via OCS
    `/cloud/user`, authenticated with the bearer token itself, through a
    dedicated short-lived client (never `self.http_client`, which is shared
    across every user's verification and would replay one user's Nextcloud
    session cookie onto another's lookup).
    """

    def _ocs_response(self, uid: str):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "ocs": {"meta": {"statuscode": 200}, "data": {"id": uid}}
        }
        return response

    def _mock_ocs_client(self, response=None, *, side_effect=None):
        """A mocked `nextcloud_httpx_client` context manager, matching the
        pattern `api/passwords.py`'s own OCS-call tests already use."""
        mock_get = AsyncMock()
        if side_effect is not None:
            mock_get.side_effect = side_effect
        else:
            mock_get.return_value = response
        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        # Must resolve falsy: a truthy __aexit__ return SUPPRESSES an
        # exception raised inside the `async with` block (real
        # httpx.AsyncClient.__aexit__ returns None), so a naive AsyncMock()
        # here would swallow the network-error/unexpected-error cases before
        # they ever reach the method's own except clause.
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client, mock_get

    async def test_resolves_to_canonical_uid(self, base_settings):
        verifier = UnifiedTokenVerifier(base_settings)
        mock_client, mock_get = self._mock_ocs_client(self._ocs_response("alice"))

        with patch(
            "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
            return_value=mock_client,
        ):
            result = await verifier._resolve_canonical_uid(
                "bearer-token", "f47ac10b-58cc-4372-a567-0e02b2c3d479"
            )

        assert result == "alice"
        mock_get.assert_awaited_once()
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer bearer-token"

    async def test_falls_back_to_claimed_uid_on_network_error(self, base_settings):
        verifier = UnifiedTokenVerifier(base_settings)
        mock_client, _ = self._mock_ocs_client(
            side_effect=httpx.ConnectError("connection refused")
        )

        with patch(
            "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
            return_value=mock_client,
        ):
            result = await verifier._resolve_canonical_uid(
                "bearer-token", "f47ac10b-58cc-4372-a567-0e02b2c3d479"
            )

        assert result == "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    async def test_falls_back_to_claimed_uid_on_unexpected_error(self, base_settings):
        """Not just `httpx.RequestError`: any failure from the lookup must
        degrade to the claimed value, since the fallback is safe by
        construction and the caller's own `except Exception` would otherwise
        reject a token JWT/introspection already validated (e.g. a malformed
        `NEXTCLOUD_HOST` raising `httpx.InvalidURL`, which is not a
        `RequestError`)."""
        verifier = UnifiedTokenVerifier(base_settings)
        mock_client, _ = self._mock_ocs_client(side_effect=httpx.InvalidURL("bad host"))

        with patch(
            "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
            return_value=mock_client,
        ):
            result = await verifier._resolve_canonical_uid(
                "bearer-token", "f47ac10b-58cc-4372-a567-0e02b2c3d479"
            )

        assert result == "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    async def test_falls_back_to_claimed_uid_on_non_200(self, base_settings):
        verifier = UnifiedTokenVerifier(base_settings)
        response = MagicMock()
        response.status_code = 401
        mock_client, _ = self._mock_ocs_client(response)

        with patch(
            "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
            return_value=mock_client,
        ):
            result = await verifier._resolve_canonical_uid("bearer-token", "some-sub")

        assert result == "some-sub"

    async def test_falls_back_to_claimed_uid_on_malformed_body(self, base_settings):
        verifier = UnifiedTokenVerifier(base_settings)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"ocs": {"data": []}}  # not a dict
        mock_client, _ = self._mock_ocs_client(response)

        with patch(
            "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
            return_value=mock_client,
        ):
            result = await verifier._resolve_canonical_uid("bearer-token", "some-sub")

        assert result == "some-sub"

    async def test_falls_back_to_claimed_uid_on_non_json_body(self, base_settings):
        verifier = UnifiedTokenVerifier(base_settings)
        response = MagicMock()
        response.status_code = 200
        response.json.side_effect = ValueError("not JSON")
        mock_client, _ = self._mock_ocs_client(response)

        with patch(
            "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
            return_value=mock_client,
        ):
            result = await verifier._resolve_canonical_uid("bearer-token", "some-sub")

        assert result == "some-sub"

    async def test_skips_lookup_without_nextcloud_host(self, base_settings):
        base_settings.nextcloud_host = None
        verifier = UnifiedTokenVerifier(base_settings)
        mock_client, mock_get = self._mock_ocs_client(self._ocs_response("alice"))

        with patch(
            "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
            return_value=mock_client,
        ):
            result = await verifier._resolve_canonical_uid("bearer-token", "some-sub")

        assert result == "some-sub"
        mock_get.assert_not_awaited()

    async def test_verify_mcp_audience_stores_canonical_uid_not_raw_sub(
        self, base_settings
    ):
        """End-to-end: a token whose `sub` is a Keycloak UUID must resolve to
        the Nextcloud UID in AccessToken.resource, not the raw claim.

        This reproduces #1326: on `main`, `access_token.resource` here comes
        back as the raw UUID, and every downstream consumer (Qdrant filters,
        `api/management.py`'s `user_id`) is keyed on an identity Nextcloud
        itself does not use.
        """
        verifier = UnifiedTokenVerifier(base_settings)
        raw_sub = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        introspection_response = {
            "active": True,
            "sub": raw_sub,
            "aud": ["test-client-id"],
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
            "client_id": "test-client-id",
        }
        mock_client, _ = self._mock_ocs_client(self._ocs_response("alice"))

        with (
            patch(
                "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
                return_value=mock_client,
            ),
            patch.object(
                verifier, "_introspect_token", return_value=introspection_response
            ),
        ):
            result = await verifier._verify_mcp_audience("opaque-token-12345")

        assert result is not None
        assert result.resource == "alice"
        assert result.resource != raw_sub

    async def test_canonical_uid_replaces_preferred_username_in_cache_too(
        self, base_settings
    ):
        """The cached entry must not carry the stale raw `preferred_username`
        claim alongside the resolved `sub`: two identity fields disagreeing
        about who the token belongs to is a live trap for any future reader
        that prefers `preferred_username`."""
        verifier = UnifiedTokenVerifier(base_settings)
        raw_sub = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        introspection_response = {
            "active": True,
            "sub": raw_sub,
            "preferred_username": raw_sub,
            "aud": ["test-client-id"],
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
            "client_id": "test-client-id",
        }
        mock_client, _ = self._mock_ocs_client(self._ocs_response("alice"))

        with (
            patch(
                "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
                return_value=mock_client,
            ),
            patch.object(
                verifier, "_introspect_token", return_value=introspection_response
            ),
        ):
            await verifier._verify_mcp_audience("opaque-token-12345")

        cache_key = hashlib.sha256(b"opaque-token-12345").hexdigest()
        cached_payload, _ = verifier._token_cache[cache_key]
        assert cached_payload["sub"] == "alice"
        assert "preferred_username" not in cached_payload

    async def test_management_api_path_also_stores_canonical_uid(
        self, monkeypatch, base_settings
    ):
        """The management-API verification path shares the same fix."""
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "test-client-id")
        from nextcloud_mcp_server.config import _reload_config

        _reload_config()
        verifier = UnifiedTokenVerifier(base_settings)
        raw_sub = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        introspection_response = {
            "active": True,
            "sub": raw_sub,
            "client_id": "test-client-id",
            "scope": "openid profile",
            "exp": int(time.time() + 3600),
        }
        mock_client, mock_get = self._mock_ocs_client(self._ocs_response("alice"))

        with (
            patch(
                "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
                return_value=mock_client,
            ),
            patch.object(
                verifier, "_introspect_token", return_value=introspection_response
            ),
        ):
            result = await verifier.verify_token_for_management_api(
                "opaque-token-12345"
            )

        assert result is not None
        assert result.resource == "alice"

        # A second call must hit the cache, not the OCS endpoint again, and
        # must still carry the resolved UID (not the raw claim).
        mock_get.reset_mock()
        with (
            patch(
                "nextcloud_mcp_server.auth.unified_verifier.nextcloud_httpx_client",
                return_value=mock_client,
            ),
            patch.object(
                verifier, "_introspect_token", return_value=introspection_response
            ),
        ):
            cached_result = await verifier.verify_token_for_management_api(
                "opaque-token-12345"
            )
        assert cached_result.resource == "alice"
        mock_get.assert_not_awaited()


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

    async def test_cache_hit_resolves_azp_against_allowlist(
        self, monkeypatch, base_settings
    ):
        """The cache-hit path must resolve `azp` too, not just fresh validation.

        This is the hot path in practice — Astrolabe polls the management API
        often enough that most validations are served from the cache — so an
        external-IdP token that passed on first validation would otherwise be
        rejected on every subsequent request.
        """
        monkeypatch.setenv("ALLOWED_MGMT_CLIENT", "nextcloud")
        from nextcloud_mcp_server.config import _reload_config

        _reload_config()
        verifier = UnifiedTokenVerifier(base_settings)
        assert verifier._allowed_mgmt_clients == {"nextcloud"}

        # Seed the cache the way _create_access_token_with_cache_key would for a
        # Keycloak token: `azp`, no `client_id` claim at all.
        token = "keycloak-token"
        cache_key = f"mgmt:{hashlib.sha256(token.encode()).hexdigest()}"
        verifier._token_cache[cache_key] = (
            {"sub": "alice", "scope": "openid profile", "azp": "nextcloud"},
            time.time() + 3600,
        )

        # No _verify_without_audience_check patch: a cache miss would try to
        # reach the IdP, so reaching the allowlist at all proves the cache hit.
        result = await verifier.verify_token_for_management_api(token)
        assert result is not None
        assert result.client_id == "nextcloud"
        assert result.resource == "alice"

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


class TestRejectionObservability:
    """Every token rejection must say *why*, and for *which client*.

    A rejection is not a routine event: it ends the MCP session and forces the
    user to authenticate again. Before this, the reason was known deep inside
    the verifier and then discarded — the metric recorded a bare "invalid" and
    several reasons were logged at DEBUG, i.e. invisible in production. The
    question "why was this client disconnected?" was unanswerable from
    telemetry alone.
    """

    VALIDATIONS = "mcp_oauth_token_validations_total"

    @staticmethod
    def _jwt_for(client_id: str | None = "mistral-client", **claims) -> str:
        payload = {"sub": "alice", **claims}
        if client_id is not None:
            payload["client_id"] = client_id
        return jwt.encode(payload, "secret", algorithm="HS256")

    @staticmethod
    def _failing_verify(exc):
        """Fail only the *verifying* decode, leaving the unverified one working.

        `_verify_jwt_signature` and `_claimed_client_id` both call `jwt.decode`;
        a blanket patch would break the client_id read too and hide the label
        under "unknown". In production they genuinely differ — PyJWT skips every
        check when `verify_signature=False`, so an expired token still yields
        its claims. The mock has to preserve that difference or the test proves
        nothing about the real path.
        """
        real_decode = jwt.decode

        def _decode(token, *args, **kwargs):
            if (kwargs.get("options") or {}).get("verify_signature") is False:
                return real_decode(token, *args, **kwargs)
            raise exc

        return _decode

    async def test_expired_jwt_records_reason_and_client(
        self, base_settings, metric_sample
    ):
        """The signature the Mistral disconnects actually produce."""
        labels = {
            "method": "jwt",
            "result": "invalid",
            "reason": "expired",
            "client_id": "mistral-client",
        }
        before = metric_sample(self.VALIDATIONS, labels)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        with patch(
            "nextcloud_mcp_server.auth.unified_verifier.jwt.decode",
            side_effect=self._failing_verify(jwt.ExpiredSignatureError("expired")),
        ):
            assert await verifier._verify_jwt_signature(self._jwt_for()) is None

        assert metric_sample(self.VALIDATIONS, labels) - before == 1

    @pytest.mark.parametrize(
        ("exc", "reason"),
        [
            (jwt.InvalidIssuerError("bad iss"), "bad_issuer"),
            (jwt.InvalidSignatureError("bad sig"), "bad_signature"),
        ],
    )
    async def test_jwt_failure_modes_are_distinguishable(
        self, base_settings, metric_sample, exc, reason
    ):
        """Expiry, a wrong issuer and a bad signature need different responses."""
        labels = {
            "method": "jwt",
            "result": "invalid",
            "reason": reason,
            "client_id": "mistral-client",
        }
        before = metric_sample(self.VALIDATIONS, labels)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        with patch(
            "nextcloud_mcp_server.auth.unified_verifier.jwt.decode",
            side_effect=self._failing_verify(exc),
        ):
            assert await verifier._verify_jwt_signature(self._jwt_for()) is None

        assert metric_sample(self.VALIDATIONS, labels) - before == 1

    async def test_inactive_introspection_records_reason(
        self, base_settings, metric_sample
    ):
        """`active=false` is the opaque-token equivalent of an expired JWT."""
        labels = {
            "method": "introspect",
            "result": "invalid",
            "reason": "inactive",
            "client_id": "unknown",
        }
        before = metric_sample(self.VALIDATIONS, labels)

        verifier = UnifiedTokenVerifier(base_settings)
        response = MagicMock(status_code=200)
        response.json.return_value = {"active": False}
        verifier.http_client.post = AsyncMock(return_value=response)

        assert await verifier._introspect_token("opaque-token") is None

        assert metric_sample(self.VALIDATIONS, labels) - before == 1

    async def test_missing_jwks_is_visible_not_debug(
        self, base_settings, metric_sample, caplog
    ):
        """A missing JWKS client rejects *every* JWT — it must not be quiet.

        This was logged at DEBUG, so the single misconfiguration that breaks
        all clients at once produced nothing at the production log level.
        """
        labels = {
            "method": "jwt",
            # "error", not "invalid": nothing is wrong with the caller's token,
            # we simply cannot validate it. That distinction is what makes the
            # result="error" alert pageable.
            "result": "error",
            "reason": "not_configured",
            "client_id": "mistral-client",
        }
        before = metric_sample(self.VALIDATIONS, labels)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = None
        with caplog.at_level(
            logging.WARNING, logger="nextcloud_mcp_server.auth.unified_verifier"
        ):
            assert await verifier._verify_jwt_signature(self._jwt_for()) is None

        assert metric_sample(self.VALIDATIONS, labels) - before == 1
        assert any("not_configured" in r.message for r in caplog.records)

    async def test_rejection_logs_client_at_warning(self, base_settings, caplog):
        """One WARNING carrying client + reason, not breadcrumbs to trace-join."""
        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        with (
            patch(
                "nextcloud_mcp_server.auth.unified_verifier.jwt.decode",
                side_effect=self._failing_verify(jwt.ExpiredSignatureError("expired")),
            ),
            caplog.at_level(
                logging.WARNING, logger="nextcloud_mcp_server.auth.unified_verifier"
            ),
        ):
            await verifier._verify_jwt_signature(self._jwt_for("acme-connector"))

        assert any(
            "acme-connector" in r.message and "expired" in r.message
            for r in caplog.records
        )

    async def test_jwks_outage_is_a_network_error_not_unknown(
        self, base_settings, metric_sample
    ):
        """A JWKS fetch failure is our IdP being unreachable, not a bad token.

        `PyJWKClientError` is not an `InvalidTokenError` subclass, so without an
        explicit clause it lands in the generic handler as "unknown" — leaving a
        JWKS outage looking different from the introspection and userinfo
        outages it is the exact peer of, on a dashboard built to tell causes
        apart.
        """
        labels = {
            "method": "jwt",
            "result": "error",
            "reason": "network_error",
            "client_id": "mistral-client",
        }
        before = metric_sample(self.VALIDATIONS, labels)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        verifier.jwks_client.get_signing_key_from_jwt.side_effect = (
            jwt.PyJWKClientError("could not fetch JWKS")
        )

        assert await verifier._verify_jwt_signature(self._jwt_for()) is None

        assert metric_sample(self.VALIDATIONS, labels) - before == 1

    async def test_empty_jwks_is_no_signing_keys_not_unknown(
        self, base_settings, metric_sample
    ):
        """An IdP publishing no keys is a misconfiguration, not an unknown error.

        `PyJWKSetError` derives straight from `PyJWTError`, NOT from the
        `PyJWKClientError` the outage clause catches, so it used to fall through
        to the generic handler as "unknown" — indistinguishable from a real
        internal bug. The operator saw a bare 401 and had to read server logs to
        find out their provider was signing with HS256.
        """
        labels = {
            "method": "jwt",
            "result": "error",
            "reason": "no_signing_keys",
            "client_id": "mistral-client",
        }
        before = metric_sample(self.VALIDATIONS, labels)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        verifier.jwks_client.uri = "https://idp.example/jwks"
        verifier.jwks_client.get_signing_key_from_jwt.side_effect = jwt.PyJWKSetError(
            "The JWK Set did not contain any keys"
        )

        assert await verifier._verify_jwt_signature(self._jwt_for()) is None

        assert metric_sample(self.VALIDATIONS, labels) - before == 1

    async def test_empty_jwks_rejection_names_the_endpoint(self, base_settings, caplog):
        """The log must point at the JWKS URI — that is the thing to go fix."""
        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        verifier.jwks_client.uri = "https://idp.example/application/o/nc/jwks/"
        verifier.jwks_client.get_signing_key_from_jwt.side_effect = jwt.PyJWKSetError(
            "The JWK Set did not contain any keys"
        )

        with caplog.at_level(
            logging.WARNING, logger="nextcloud_mcp_server.auth.unified_verifier"
        ):
            assert await verifier._verify_jwt_signature(self._jwt_for()) is None

        assert any(
            "https://idp.example/application/o/nc/jwks/" in r.message
            for r in caplog.records
        )

    async def test_real_pyjwt_raises_pyjwksseterror_on_an_empty_key_set(
        self, base_settings
    ):
        """Pin the upstream behaviour the clause above depends on.

        If PyJWT ever re-parents `PyJWKSetError` under `PyJWKClientError`, or
        changes which error an empty key set raises, this fix silently reverts
        to reporting "network_error"/"unknown" — so assert against the real
        library rather than only our mock of it.
        """
        assert not issubclass(jwt.PyJWKSetError, jwt.PyJWKClientError)

        with pytest.raises(jwt.PyJWKSetError):
            jwt.PyJWKSet.from_dict({"keys": []})

    def test_empty_verified_client_id_falls_back(self, base_settings, metric_sample):
        """An empty verified id recovers azp/aud rather than recording nothing.

        A verified payload can carry `azp`/`aud` with no top-level `client_id`,
        and for a JWT the fallback re-reads the same already-verified bytes.
        Honouring "" strictly would lose a recoverable identity for no gain.
        """
        labels = {
            "method": "allowlist",
            "result": "invalid",
            "reason": "not_allowlisted",
            "client_id": "via-azp",
        }
        before = metric_sample(self.VALIDATIONS, labels)

        verifier = UnifiedTokenVerifier(base_settings)
        token = jwt.encode({"sub": "a", "azp": "via-azp"}, "s", algorithm="HS256")
        verifier._reject("allowlist", "not_allowlisted", token, client_id="")

        assert metric_sample(self.VALIDATIONS, labels) - before == 1

    async def test_bad_audience_records_one_event_not_two(
        self, base_settings, metric_sample
    ):
        """A token refused on audience is not also counted as valid.

        The signature verifying and the token being *accepted* are different
        events. Recording "valid" per validation stage and then "bad_audience"
        at the refusal made one token produce two increments, inflating
        `result="valid"` relative to tokens actually accepted — and every ratio
        query in docs/observability.md is built on that denominator.
        """
        rejected = {
            "method": "jwt",
            "result": "invalid",
            "reason": "bad_audience",
            "client_id": "mistral-client",
        }
        accepted = {
            "method": "jwt",
            "result": "valid",
            "reason": "none",
            "client_id": "unknown",
        }
        before_rejected = metric_sample(self.VALIDATIONS, rejected)
        before_accepted = metric_sample(self.VALIDATIONS, accepted)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        # Signature verifies, but the audience is somebody else's.
        with patch.object(
            verifier,
            "_verify_jwt_signature",
            AsyncMock(return_value={"sub": "alice", "aud": ["someone-else"]}),
        ):
            assert await verifier._verify_mcp_audience(self._jwt_for()) is None

        assert metric_sample(self.VALIDATIONS, rejected) - before_rejected == 1
        assert metric_sample(self.VALIDATIONS, accepted) == before_accepted

    async def test_accepted_token_records_valid_once(
        self, base_settings, metric_sample
    ):
        """The accepted path still records exactly one `valid` event."""
        accepted = {
            "method": "jwt",
            "result": "valid",
            "reason": "none",
            "client_id": "unknown",
        }
        before = metric_sample(self.VALIDATIONS, accepted)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        with patch.object(
            verifier,
            "_verify_jwt_signature",
            AsyncMock(
                return_value={
                    "sub": "alice",
                    "aud": ["test-client-id"],
                    "exp": int(time.time()) + 600,
                }
            ),
        ):
            assert await verifier._verify_mcp_audience(self._jwt_for()) is not None

        assert metric_sample(self.VALIDATIONS, accepted) - before == 1

    @pytest.mark.parametrize(
        ("surface", "method_name", "kwargs"),
        [
            ("mcp", "_verify_mcp_audience", {}),
            (
                "management",
                "_verify_without_audience_check",
                {"cache_key": "mgmt:nosub"},
            ),
        ],
    )
    async def test_no_phantom_valid_when_token_creation_fails(
        self, base_settings, metric_sample, surface, method_name, kwargs
    ):
        """A token that yields no AccessToken is a rejection, not an acceptance.

        `_create_access_token*` returns None — without raising — for a payload
        carrying no `sub`/`preferred_username`. Recording "valid" before that
        point meant a signature- and audience-valid token could be counted as
        accepted while the caller got None, the session ended and the user was
        sent back through login: the exact silent-rejection shape this work
        exists to remove, surviving in the one place round 6's fix didn't reach.

        Checked on both surfaces because they had drifted apart — the
        management path was still recording per-stage.
        """
        accepted = {
            "method": "jwt",
            "result": "valid",
            "reason": "none",
            "client_id": "unknown",
        }
        rejected = {
            "method": "jwt",
            "result": "error",
            "reason": "unknown",
            "client_id": "mistral-client",
        }
        before_accepted = metric_sample(self.VALIDATIONS, accepted)
        before_rejected = metric_sample(self.VALIDATIONS, rejected)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        # Signature and audience are fine; the payload simply names no user.
        with patch.object(
            verifier,
            "_verify_jwt_signature",
            AsyncMock(return_value={"aud": ["test-client-id"]}),
        ):
            assert (
                await getattr(verifier, method_name)(self._jwt_for(), **kwargs) is None
            )

        assert metric_sample(self.VALIDATIONS, accepted) == before_accepted
        assert metric_sample(self.VALIDATIONS, rejected) - before_rejected == 1

    async def test_missing_sub_rejection_logs_once(self, base_settings, caplog):
        """One line per rejection — this path briefly logged ERROR *and* WARNING.

        Introduced by the round-7 fix: routing the None result through _reject()
        added a WARNING on top of the pre-existing ERROR inside
        _create_access_token_with_cache_key. The round-7 test asserted only on
        the metric, so it passed while the duplicate went in — the same blind
        spot test_bad_audience_logs_once exists to prevent on the other path.
        """
        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        with (
            patch.object(
                verifier,
                "_verify_jwt_signature",
                AsyncMock(return_value={"aud": ["test-client-id"]}),
            ),
            caplog.at_level(
                logging.DEBUG, logger="nextcloud_mcp_server.auth.unified_verifier"
            ),
        ):
            assert await verifier._verify_mcp_audience(self._jwt_for()) is None

        lines = [r for r in caplog.records if "sub" in r.message]
        assert len(lines) == 1, [r.message for r in lines]

    @pytest.mark.parametrize(
        ("allowlist", "reason", "result"),
        [
            # Empty allowlist rejects every client — our misconfiguration.
            (frozenset(), "not_configured", "error"),
            # Populated allowlist, this client absent — the caller's problem.
            (frozenset({"astrolabe"}), "not_allowlisted", "invalid"),
        ],
    )
    async def test_empty_allowlist_is_our_fault_not_the_callers(
        self, base_settings, metric_sample, allowlist, reason, result
    ):
        """ "Nobody is allowed" and "you are not allowed" need different responses.

        An unset ALLOWED_MGMT_CLIENT breaks every management client at once —
        the same shape as the JWKS/introspection `not_configured` cases carved
        out as pageable. Reporting it as the caller's invalid token would keep
        it out of the result="error" alert that exists to catch exactly this.
        """
        labels = {
            "method": "allowlist",
            "result": result,
            "reason": reason,
            "client_id": "not-on-the-list",
        }
        before = metric_sample(self.VALIDATIONS, labels)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier._allowed_mgmt_clients = allowlist
        token = self._jwt_for("not-on-the-list")
        with patch.object(
            verifier,
            "_verify_without_audience_check",
            AsyncMock(
                return_value=AccessToken(
                    token=token,
                    client_id="not-on-the-list",
                    scopes=[],
                    expires_at=int(time.time()) + 600,
                )
            ),
        ):
            assert await verifier.verify_token_for_management_api(token) is None

        assert metric_sample(self.VALIDATIONS, labels) - before == 1

    async def test_fallback_chain_records_one_rejection(
        self, base_settings, metric_sample, caplog
    ):
        """Two validators, one token, one rejection.

        With both JWKS and introspection configured — the ordinary
        defence-in-depth deployment — an expired JWT is refused twice: the
        signature check sees `expired`, then introspection sees `inactive` for
        the same token. Recording each stage independently rebuilt the exact
        "two breadcrumbs to correlate" problem this work exists to remove, and
        made it louder by promoting both lines to WARNING.
        """
        jwt_labels = {
            "method": "jwt",
            "result": "invalid",
            "reason": "expired",
            "client_id": "mistral-client",
        }
        introspect_labels = {
            "method": "introspect",
            "result": "invalid",
            "reason": "inactive",
            "client_id": "mistral-client",
        }
        before_jwt = metric_sample(self.VALIDATIONS, jwt_labels)
        before_introspect = metric_sample(self.VALIDATIONS, introspect_labels)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {"active": False}
        verifier.http_client.post = AsyncMock(return_value=response)

        with (
            patch(
                "nextcloud_mcp_server.auth.unified_verifier.jwt.decode",
                side_effect=self._failing_verify(jwt.ExpiredSignatureError("expired")),
            ),
            caplog.at_level(
                logging.WARNING, logger="nextcloud_mcp_server.auth.unified_verifier"
            ),
        ):
            assert await verifier._verify_mcp_audience(self._jwt_for()) is None

        # Attributed to the validator that actually produced the 401...
        assert (
            metric_sample(self.VALIDATIONS, introspect_labels) - before_introspect == 1
        )
        # ...and the earlier stage is not a second event.
        assert metric_sample(self.VALIDATIONS, jwt_labels) == before_jwt

        rejections = [r for r in caplog.records if "Token rejected" in r.message]
        assert len(rejections) == 1, [r.message for r in rejections]
        # The whole story survives on that one line.
        assert "jwt/expired" in rejections[0].message

    async def test_accepted_after_fallback_records_no_rejection(
        self, base_settings, metric_sample
    ):
        """A token the fallback rescues was never rejected.

        JWT verification failing and introspection then succeeding is one
        accepted token. Recording the failed stage made an accepted token
        produce a rejection *and* a valid — inflating both sides of every
        ratio in docs/observability.md at once.
        """
        rejected = {
            "method": "jwt",
            "result": "invalid",
            "reason": "expired",
            "client_id": "mistral-client",
        }
        accepted = {
            "method": "introspect",
            "result": "valid",
            "reason": "none",
            "client_id": "unknown",
        }
        before_rejected = metric_sample(self.VALIDATIONS, rejected)
        before_accepted = metric_sample(self.VALIDATIONS, accepted)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier.jwks_client = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "active": True,
            "sub": "alice",
            "aud": ["test-client-id"],
        }
        verifier.http_client.post = AsyncMock(return_value=response)

        with patch(
            "nextcloud_mcp_server.auth.unified_verifier.jwt.decode",
            side_effect=self._failing_verify(jwt.ExpiredSignatureError("expired")),
        ):
            assert await verifier._verify_mcp_audience(self._jwt_for()) is not None

        assert metric_sample(self.VALIDATIONS, rejected) == before_rejected
        assert metric_sample(self.VALIDATIONS, accepted) - before_accepted == 1

    async def test_allowlist_rejection_records_no_valid_end_to_end(
        self, base_settings, metric_sample
    ):
        """A request refused by the allowlist is not also counted as served.

        Deliberately drives the REAL `_verify_without_audience_check` rather
        than mocking it. Every other allowlist test patches that method out,
        so its `valid` record never ran and the double-count was invisible —
        the tests asserted on the rejection they set up and never saw the
        acceptance they also caused.

        Authentication succeeding is not the request being granted: the
        allowlist still has a say, and until it has spoken nothing has been
        served.
        """
        accepted_jwt = {
            "method": "jwt",
            "result": "valid",
            "reason": "none",
            "client_id": "unknown",
        }
        rejected = {
            "method": "allowlist",
            "result": "invalid",
            "reason": "not_allowlisted",
            "client_id": "not-on-the-list",
        }
        before_accepted = metric_sample(self.VALIDATIONS, accepted_jwt)
        before_rejected = metric_sample(self.VALIDATIONS, rejected)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier._allowed_mgmt_clients = frozenset({"astrolabe"})
        verifier.jwks_client = MagicMock()
        token = self._jwt_for("not-on-the-list")
        # Signature verifies and a token is created — only the allowlist refuses.
        with patch.object(
            verifier,
            "_verify_jwt_signature",
            AsyncMock(
                return_value={
                    "sub": "alice",
                    "client_id": "not-on-the-list",
                    "exp": int(time.time()) + 600,
                }
            ),
        ):
            assert await verifier.verify_token_for_management_api(token) is None

        assert metric_sample(self.VALIDATIONS, rejected) - before_rejected == 1
        assert metric_sample(self.VALIDATIONS, accepted_jwt) == before_accepted

    async def test_allowlisted_request_records_valid_once(
        self, base_settings, metric_sample
    ):
        """The granted path still records exactly one `valid`, once cleared."""
        accepted = {
            "method": "jwt",
            "result": "valid",
            "reason": "none",
            "client_id": "astrolabe",
        }
        before = metric_sample(self.VALIDATIONS, accepted)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier._allowed_mgmt_clients = frozenset({"astrolabe"})
        verifier.jwks_client = MagicMock()
        with patch.object(
            verifier,
            "_verify_jwt_signature",
            AsyncMock(
                return_value={
                    "sub": "alice",
                    "client_id": "astrolabe",
                    "exp": int(time.time()) + 600,
                }
            ),
        ):
            granted = await verifier.verify_token_for_management_api(
                self._jwt_for("astrolabe")
            )

        assert granted is not None
        assert metric_sample(self.VALIDATIONS, accepted) - before == 1

    async def test_cache_hit_does_not_rerecord_valid(
        self, base_settings, metric_sample
    ):
        """A polled token is validated once, not once per request.

        Astrolabe polls the management API continuously with the same token.
        Recording `valid` on every cache hit would make the metric count
        *requests* rather than *validations*, so the denominator would drift
        with polling frequency rather than with anything meaningful.

        The previous commit asserted this invariant in its message and left it
        resting on a manual trace. Given this PR has five separate instances of
        a test watching the wrong signal, an untested claim is the one shape it
        should not ship.
        """
        accepted = {
            "method": "jwt",
            "result": "valid",
            "reason": "none",
            "client_id": "astrolabe",
        }
        before = metric_sample(self.VALIDATIONS, accepted)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier._allowed_mgmt_clients = frozenset({"astrolabe"})
        verifier.jwks_client = MagicMock()
        token = self._jwt_for("astrolabe")
        verify = AsyncMock(
            return_value={
                "sub": "alice",
                "client_id": "astrolabe",
                "exp": int(time.time()) + 600,
            }
        )
        with patch.object(verifier, "_verify_jwt_signature", verify):
            first = await verifier.verify_token_for_management_api(token)
            second = await verifier.verify_token_for_management_api(token)

        assert first is not None and second is not None
        # Second call was served from cache — the validator never ran again...
        assert verify.await_count == 1
        # ...and the grant was not counted twice.
        assert metric_sample(self.VALIDATIONS, accepted) - before == 1

    def test_opaque_token_client_id_degrades_to_unknown(self, base_settings):
        """An opaque token carries no readable client_id — don't invent one."""
        verifier = UnifiedTokenVerifier(base_settings)
        assert verifier._claimed_client_id("not-a-jwt") is None

    def test_client_id_falls_back_through_claims(self, base_settings):
        """Not every issuer uses `client_id`; azp and aud are the usual others."""
        verifier = UnifiedTokenVerifier(base_settings)
        azp = jwt.encode({"sub": "a", "azp": "via-azp"}, "s", algorithm="HS256")
        aud = jwt.encode({"sub": "a", "aud": ["via-aud"]}, "s", algorithm="HS256")
        assert verifier._claimed_client_id(azp) == "via-azp"
        assert verifier._claimed_client_id(aud) == "via-aud"

    @pytest.mark.parametrize(
        ("reason", "expected_result"),
        [
            ("expired", "invalid"),
            ("inactive", "invalid"),
            ("bad_signature", "invalid"),
            ("bad_issuer", "invalid"),
            ("bad_audience", "invalid"),
            ("not_allowlisted", "invalid"),
            ("not_configured", "error"),
            ("network_error", "error"),
            ("unknown", "error"),
        ],
    )
    def test_result_is_derived_from_reason(
        self, base_settings, metric_sample, reason, expected_result
    ):
        """`result` splits blame, and must not be settable independently.

        It used to be a `_reject()` parameter defaulting to "invalid", and 4 of
        the 6 `not_configured` call sites silently took the default — putting
        "no validator configured at all", the one failure that rejects every
        client's every token, on the *caller's* side of the split. Deriving it
        from `reason` makes that class of drift impossible rather than merely
        fixed once.
        """
        labels = {
            "method": "jwt",
            "result": expected_result,
            "reason": reason,
            "client_id": "mistral-client",
        }
        other = "invalid" if expected_result == "error" else "error"
        before = metric_sample(self.VALIDATIONS, labels)
        before_other = metric_sample(self.VALIDATIONS, {**labels, "result": other})

        verifier = UnifiedTokenVerifier(base_settings)
        verifier._reject("jwt", reason, self._jwt_for())

        assert metric_sample(self.VALIDATIONS, labels) - before == 1
        assert metric_sample(self.VALIDATIONS, {**labels, "result": other}) == (
            before_other
        )

    @pytest.mark.parametrize(
        ("token_kind", "make_token"),
        [
            ("jwt", lambda self: self._jwt_for("not-on-the-list")),
            # The realistic case for this path: Astrolabe's management tokens
            # are opaque, so nothing can be recovered from the raw bytes. The
            # JWT-shaped case masked the bug — _claimed_client_id happened to
            # succeed and the metric looked correct.
            ("opaque", lambda self: "opaque-token-no-dots"),
        ],
    )
    async def test_management_allowlist_rejection_is_recorded(
        self, base_settings, metric_sample, token_kind, make_token
    ):
        """An allowlist rejection disconnects a client like any other.

        It is authorization rather than validation, but from the operator's
        side it is indistinguishable — a client that suddenly stops working —
        and the client_id is right there, so it goes through the same funnel.

        Parametrized over token *shape* because the two differ in where the
        identity can be read from. By this point the caller holds a verified
        `access_token.client_id`; re-deriving it from the raw bytes only works
        for a JWT, so an opaque token would record `client_id="unknown"` —
        and opaque is exactly what Astrolabe's management tokens are.
        """
        labels = {
            "method": "allowlist",
            "result": "invalid",
            "reason": "not_allowlisted",
            "client_id": "not-on-the-list",
        }
        before = metric_sample(self.VALIDATIONS, labels)

        verifier = UnifiedTokenVerifier(base_settings)
        verifier._allowed_mgmt_clients = frozenset({"astrolabe"})
        token = make_token(self)
        with patch.object(
            verifier,
            "_verify_without_audience_check",
            AsyncMock(
                return_value=AccessToken(
                    token=token,
                    client_id="not-on-the-list",
                    scopes=[],
                    expires_at=int(time.time()) + 600,
                )
            ),
        ):
            assert await verifier.verify_token_for_management_api(token) is None

        assert metric_sample(self.VALIDATIONS, labels) - before == 1

    def test_bad_audience_logs_once(self, base_settings, caplog):
        """One line per rejection — this path used to log ERROR *and* WARNING."""
        verifier = UnifiedTokenVerifier(base_settings)
        with caplog.at_level(
            logging.DEBUG, logger="nextcloud_mcp_server.auth.unified_verifier"
        ):
            verifier._reject("jwt", "bad_audience", self._jwt_for(), "got []")

        assert len([r for r in caplog.records if "bad_audience" in r.message]) == 1

    async def test_opaque_no_validator_configured_is_recorded(
        self, base_settings, metric_sample
    ):
        """The last silent rejection path: an opaque token with no validator.

        The management-API twin of the `jwks_client is None` branch this work
        started from — and quieter still, since that one at least logged at
        DEBUG while this returned None with no metric and no log at all.
        """
        labels = {
            "method": "unknown",
            "result": "error",
            "reason": "not_configured",
            "client_id": "unknown",
        }
        before = metric_sample(self.VALIDATIONS, labels)

        base_settings.introspection_uri = None
        base_settings.userinfo_uri = None
        verifier = UnifiedTokenVerifier(base_settings)

        assert (
            await verifier._verify_without_audience_check("opaque-token", "mgmt:none")
            is None
        )

        assert metric_sample(self.VALIDATIONS, labels) - before == 1

    async def test_failed_validator_is_not_double_counted(
        self, base_settings, metric_sample
    ):
        """A validator that ran and failed already recorded it — don't record twice.

        The catch-all `payload is None` is reached both when nothing was tried
        and when something was tried and failed. Only the first is silent; the
        naive fix records both and inflates every introspection failure into two
        events attributed to different methods.
        """
        no_validator = {
            "method": "unknown",
            "result": "error",
            "reason": "not_configured",
            "client_id": "unknown",
        }
        inactive = {
            "method": "introspect",
            "result": "invalid",
            "reason": "inactive",
            "client_id": "unknown",
        }
        before_nv = metric_sample(self.VALIDATIONS, no_validator)
        before_inactive = metric_sample(self.VALIDATIONS, inactive)

        base_settings.userinfo_uri = None
        verifier = UnifiedTokenVerifier(base_settings)
        response = MagicMock(status_code=200)
        response.json.return_value = {"active": False}
        verifier.http_client.post = AsyncMock(return_value=response)

        assert (
            await verifier._verify_without_audience_check("opaque-token", "mgmt:x")
            is None
        )

        # Exactly one event, from _introspect_token, attributed to introspect.
        assert metric_sample(self.VALIDATIONS, inactive) - before_inactive == 1
        assert metric_sample(self.VALIDATIONS, no_validator) == before_nv
