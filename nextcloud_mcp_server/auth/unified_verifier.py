"""
Unified Token Verifier for ADR-005 Token Audience Validation.

This module replaces both NextcloudTokenVerifier and ProgressiveConsentTokenVerifier
with a single implementation that supports two compliant OAuth modes:

1. Multi-audience mode (default): Validates MCP audience per RFC 7519 (resource servers
   validate only their own audience). Nextcloud independently validates its own audience.
2. Token exchange mode (opt-in): Tokens have MCP audience only, exchanged for Nextcloud tokens

Key Design Principles:
- Token verification happens HERE (validates MCP audience per OAuth spec)
- Token exchange happens in context_helper.py (when creating NextcloudClient)
- No token passthrough allowed (complies with MCP Security Specification)
- Token reuse IS allowed for multi-audience tokens (RFC 8707)
"""

import hashlib
import logging
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.observability.metrics import (
    oauth_token_cache_hits_total,
    record_oauth_token_validation,
)

logger = logging.getLogger(__name__)


class UnifiedTokenVerifier(TokenVerifier):
    """
    Unified token verifier supporting both multi-audience and token exchange modes.
    Compliant with MCP security specification - no token pass-through.

    This verifier:
    1. Validates tokens using JWT verification with JWKS or introspection fallback
    2. Enforces proper audience validation based on configured mode
    3. Caches successful validations to avoid repeated API calls

    Mode Selection (via ENABLE_TOKEN_EXCHANGE setting):
    - False/omit (default): Multi-audience mode - validates MCP audience only (per RFC 7519).
      Nextcloud independently validates its own audience when receiving API calls.
    - True: Exchange mode - requires MCP audience only, then exchanges for Nextcloud token
    """

    def __init__(self, settings: Settings):
        """
        Initialize the unified token verifier.

        Args:
            settings: Application settings containing OAuth configuration
        """
        self.settings = settings
        self.mode = "exchange" if settings.enable_token_exchange else "multi-audience"

        # Common components for all modes
        self.http_client = httpx.AsyncClient(timeout=10.0)

        # JWT verification support
        self.jwks_client: PyJWKClient | None = None
        if hasattr(settings, "jwks_uri") and settings.jwks_uri:
            logger.info(f"JWT verification enabled with JWKS URI: {settings.jwks_uri}")
            self.jwks_client = PyJWKClient(settings.jwks_uri, cache_keys=True)

        # Introspection support (for opaque tokens)
        self.introspection_uri: str | None = None
        if (
            hasattr(settings, "introspection_uri")
            and settings.introspection_uri
            and settings.oidc_client_id
            and settings.oidc_client_secret
        ):
            self.introspection_uri = settings.introspection_uri
            logger.info(f"Token introspection enabled: {self.introspection_uri}")

        # Token cache: token_hash -> (userinfo, expiry_timestamp)
        self._token_cache: dict[str, tuple[dict[str, Any], float]] = {}
        self.cache_ttl = 3600  # 1 hour default

        logger.info(
            f"UnifiedTokenVerifier initialized in {self.mode} mode. "
            f"MCP audience: {settings.oidc_client_id} or {settings.nextcloud_mcp_server_url}, "
            f"Nextcloud resource URI: {settings.nextcloud_resource_uri}"
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Verify token according to MCP TokenVerifier protocol.

        Per RFC 7519, we validate only MCP audience. The mode determines what
        happens AFTER verification in context_helper.py:
        - Multi-audience mode: Use token directly (Nextcloud validates its own audience)
        - Exchange mode: Exchange for Nextcloud-audience token via RFC 8693

        Args:
            token: Bearer token to verify

        Returns:
            AccessToken if valid with MCP audience, None otherwise
        """
        # Check cache first
        cached = self._get_cached_token(token)
        if cached:
            logger.debug("Token found in cache")
            oauth_token_cache_hits_total.labels(hit="true").inc()
            return cached

        oauth_token_cache_hits_total.labels(hit="false").inc()

        # Both modes do the same validation (MCP audience only)
        return await self._verify_mcp_audience(token)

    async def _verify_mcp_audience(self, token: str) -> AccessToken | None:
        """
        Validate token has MCP audience.

        Per RFC 7519 Section 4.1.3, resource servers validate only their own
        presence in the audience claim. We don't validate Nextcloud's audience -
        that's Nextcloud's responsibility when it receives the token.

        Args:
            token: Bearer token to verify

        Returns:
            AccessToken if valid with MCP audience, None otherwise
        """
        validation_method = "unknown"
        try:
            # Attempt JWT verification first
            if self._is_jwt_format(token) and self.jwks_client:
                validation_method = "jwt"
                payload = await self._verify_jwt_signature(token)
                if payload:
                    record_oauth_token_validation("jwt", "valid")
                else:
                    record_oauth_token_validation("jwt", "invalid")
            else:
                # Fall back to introspection for opaque tokens
                validation_method = "introspect"
                payload = await self._introspect_token(token)
                if payload:
                    record_oauth_token_validation("introspect", "valid")
                else:
                    record_oauth_token_validation("introspect", "invalid")
                if not payload:
                    return None

            # Check payload is valid
            if not payload:
                return None

            # Validate MCP audience is present
            if not self._has_mcp_audience(payload):
                audiences = payload.get("aud", [])
                logger.error(
                    f"Token rejected: Missing MCP audience. "
                    f"Got {audiences}, need MCP ({self.settings.oidc_client_id} or "
                    f"{self.settings.nextcloud_mcp_server_url})"
                )
                # Record as invalid due to audience mismatch
                record_oauth_token_validation(validation_method, "invalid")
                return None

            # Log based on mode for clarity
            if self.mode == "multi-audience":
                logger.info(
                    "MCP audience validated - token can be used directly "
                    "(Nextcloud will validate its own audience)"
                )
            else:
                logger.info(
                    "MCP audience validated - token will be exchanged for Nextcloud access"
                )

            return self._create_access_token(token, payload)

        except Exception as e:
            logger.error(f"Token verification failed: {e}")
            record_oauth_token_validation(validation_method, "error")
            return None

    def _has_mcp_audience(self, payload: dict[str, Any]) -> bool:
        """
        Check if token has MCP audience.

        Per RFC 7519 Section 4.1.3, resource servers should only validate their own
        presence in the audience claim. We don't validate Nextcloud's audience - that's
        Nextcloud's responsibility when it receives the token.

        Args:
            payload: Decoded token payload

        Returns:
            True if MCP audience present, False otherwise
        """
        audiences = payload.get("aud", [])
        if isinstance(audiences, str):
            audiences = [audiences]

        audiences_set = set(audiences)

        # MCP must have at least one: client_id OR server_url OR server_url/mcp
        return bool(
            self.settings.oidc_client_id in audiences_set
            or (
                self.settings.nextcloud_mcp_server_url
                and (
                    self.settings.nextcloud_mcp_server_url in audiences_set
                    or f"{self.settings.nextcloud_mcp_server_url}/mcp" in audiences_set
                )
            )
        )

    def _is_jwt_format(self, token: str) -> bool:
        """
        Check if token looks like a JWT (has 3 parts separated by dots).

        Args:
            token: The token to check

        Returns:
            True if token appears to be JWT format
        """
        return "." in token and token.count(".") == 2

    async def _verify_jwt_signature(self, token: str) -> dict[str, Any] | None:
        """
        Verify JWT token with signature validation using JWKS.

        Args:
            token: JWT token to verify

        Returns:
            Decoded payload if valid, None if invalid
        """
        try:
            assert self.jwks_client is not None  # Caller should check before calling

            # Get signing key from JWKS
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)

            # Verify and decode JWT
            # Note: We don't validate audience here - that's done separately based on mode
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=(
                    self.settings.oidc_issuer
                    if hasattr(self.settings, "oidc_issuer")
                    else None
                ),
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": (
                        True
                        if hasattr(self.settings, "oidc_issuer")
                        and self.settings.oidc_issuer
                        else False
                    ),
                    "verify_aud": False,  # We handle audience validation separately
                },
            )

            logger.debug(f"JWT signature verified for user: {payload.get('sub')}")
            return payload

        except jwt.ExpiredSignatureError:
            logger.info("JWT token has expired")
            return None
        except jwt.InvalidIssuerError as e:
            logger.warning(f"JWT issuer validation failed: {e}")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"JWT validation failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during JWT verification: {e}")
            return None

    async def _introspect_token(self, token: str) -> dict[str, Any] | None:
        """
        Validate token by calling the introspection endpoint (RFC 7662).

        Args:
            token: Bearer token to introspect

        Returns:
            Token payload if active, None if inactive or invalid
        """
        if not self.introspection_uri:
            logger.debug("No introspection endpoint configured")
            return None

        try:
            # Introspection requires client authentication
            response = await self.http_client.post(
                self.introspection_uri,
                data={"token": token},
                auth=(self.settings.oidc_client_id, self.settings.oidc_client_secret),
            )

            if response.status_code == 200:
                introspection_data = response.json()

                # Check if token is active
                if not introspection_data.get("active", False):
                    logger.info("Token introspection returned inactive=false")
                    return None

                logger.debug(
                    f"Token introspected successfully for user: {introspection_data.get('sub')}"
                )
                return introspection_data

            elif response.status_code in (400, 401, 403):
                logger.warning(
                    f"Token introspection failed: HTTP {response.status_code}. "
                    f"Response: {response.text[:200] if response.text else 'empty'}"
                )
                return None
            else:
                logger.warning(
                    f"Unexpected response from introspection: {response.status_code}. "
                    f"Response: {response.text[:200] if response.text else 'empty'}"
                )
                return None

        except httpx.TimeoutException:
            logger.error("Timeout while introspecting token")
            return None
        except httpx.RequestError as e:
            logger.error(f"Network error while introspecting token: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during token introspection: {e}")
            return None

    def _create_access_token(
        self, token: str, payload: dict[str, Any]
    ) -> AccessToken | None:
        """
        Create AccessToken object from validated token payload.

        Args:
            token: The bearer token
            payload: Validated token payload

        Returns:
            AccessToken object or None if required fields missing
        """
        # Extract username (sub claim, with fallback to preferred_username)
        username = payload.get("sub") or payload.get("preferred_username")
        if not username:
            logger.error(
                "No 'sub' or 'preferred_username' claim found in token payload"
            )
            return None

        # Extract scopes from scope claim (space-separated string)
        scope_string = payload.get("scope", "")
        scopes = scope_string.split() if scope_string else []
        logger.debug(
            f"Extracted scopes from token - scope claim: '{scope_string}' -> scopes list: {scopes}"
        )

        # Extract expiration
        exp = payload.get("exp")
        if not exp:
            logger.warning("No 'exp' claim in token, using default TTL")
            exp = int(time.time() + self.cache_ttl)

        # Cache the result
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        userinfo = {
            "sub": username,
            "scope": scope_string,
            **{k: v for k, v in payload.items() if k not in ["sub", "scope"]},
        }
        self._token_cache[token_hash] = (userinfo, exp)

        return AccessToken(
            token=token,
            client_id=payload.get("client_id", ""),
            scopes=scopes,
            expires_at=exp,
            resource=username,  # Store username in resource field (RFC 8707)
        )

    def _get_cached_token(self, token: str) -> AccessToken | None:
        """
        Retrieve a token from cache if not expired.

        Args:
            token: The bearer token to look up

        Returns:
            AccessToken if cached and valid, None otherwise
        """
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if token_hash not in self._token_cache:
            return None

        userinfo, expiry = self._token_cache[token_hash]

        # Check if expired
        if time.time() >= expiry:
            logger.debug("Cached token expired, removing from cache")
            del self._token_cache[token_hash]
            return None

        # Return cached AccessToken
        username = userinfo.get("sub") or userinfo.get("preferred_username")
        scope_string = userinfo.get("scope", "")
        scopes = scope_string.split() if scope_string else []

        return AccessToken(
            token=token,
            client_id=userinfo.get("client_id", ""),
            scopes=scopes,
            expires_at=int(expiry),
            resource=username,
        )

    def clear_cache(self):
        """Clear the token cache."""
        self._token_cache.clear()
        logger.debug("Token cache cleared")

    async def close(self):
        """Cleanup resources."""
        await self.http_client.aclose()
        logger.debug("Unified token verifier closed")
