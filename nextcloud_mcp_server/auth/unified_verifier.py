"""
Unified Token Verifier for ADR-005 Token Audience Validation.

This module replaces both NextcloudTokenVerifier and ProgressiveConsentTokenVerifier
with a single implementation using multi-audience validation: it validates the MCP
audience per RFC 7519 (resource servers validate only their own audience), and
Nextcloud independently validates its own audience when it receives the token.

Key Design Principles:
- Token verification happens HERE (validates MCP audience per OAuth spec)
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

from nextcloud_mcp_server.config import Settings, cfg
from nextcloud_mcp_server.observability.metrics import (
    oauth_token_cache_hits_total,
    record_oauth_token_validation,
)

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


class UnifiedTokenVerifier(TokenVerifier):
    """
    Unified token verifier for multi-audience tokens (ADR-005).
    Compliant with MCP security specification - no token pass-through.

    This verifier:
    1. Validates tokens using JWT verification with JWKS or introspection fallback
    2. Enforces MCP audience validation (per RFC 7519); Nextcloud independently
       validates its own audience when receiving API calls
    3. Caches successful validations to avoid repeated API calls
    """

    def __init__(self, settings: Settings):
        """
        Initialize the unified token verifier.

        Args:
            settings: Application settings containing OAuth configuration
        """
        self.settings = settings

        # Common components for all modes
        self.http_client = nextcloud_httpx_client(timeout=10.0)

        # JWT verification support
        self.jwks_client: PyJWKClient | None = None
        if hasattr(settings, "jwks_uri") and settings.jwks_uri:
            logger.info("JWT verification enabled with JWKS URI: %s", settings.jwks_uri)
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
            logger.info("Token introspection enabled: %s", self.introspection_uri)

        # Userinfo fallback (for opaque tokens minted for a *different* OIDC
        # client, e.g. Astrolabe, which the Nextcloud oidc app's introspection
        # endpoint reports inactive cross-client). A 200 from userinfo proves
        # the bearer is a live token for its user regardless of issuing client.
        self.userinfo_uri: str | None = None
        if hasattr(settings, "userinfo_uri") and settings.userinfo_uri:
            self.userinfo_uri = settings.userinfo_uri
            logger.info(
                "Userinfo token validation fallback enabled: %s", self.userinfo_uri
            )

        # Build list of valid issuers (internal + public may differ in Docker)
        # AS proxy obtains tokens via internal URL (e.g. http://app:80), while
        # NEXTCLOUD_PUBLIC_ISSUER_URL is the browser-facing URL (e.g. http://localhost:8080)
        self.valid_issuers: list[str] = []
        if hasattr(settings, "oidc_issuer") and settings.oidc_issuer:
            self.valid_issuers.append(settings.oidc_issuer)
        if hasattr(settings, "nextcloud_host") and settings.nextcloud_host:
            host = settings.nextcloud_host.rstrip("/")
            if host not in self.valid_issuers:
                self.valid_issuers.append(host)

        # Token cache: token_hash -> (userinfo, expiry_timestamp)
        self._token_cache: dict[str, tuple[dict[str, Any], float]] = {}
        self.cache_ttl = 3600  # 1 hour default
        # Userinfo responses carry no token `exp`, so userinfo-validated opaque
        # tokens fall back to a TTL here. Keep it short: a revoked/expired token
        # is still honored from cache until this window elapses (no exp to gate).
        self.userinfo_cache_ttl = 300  # 5 minutes

        # NOTE: ALLOWED_MCP_CLIENTS and ALLOWED_MGMT_CLIENT are currently separate
        # env vars to keep the MCP-route and management-API auth surfaces
        # independent. These may be consolidated into a single env var later
        # once the deployment story stabilises.
        self._allowed_mgmt_clients: frozenset[str] = frozenset(
            entry.strip()
            for entry in cfg("ALLOWED_MGMT_CLIENT", "").split(",")
            if entry.strip()
        )
        if not self._allowed_mgmt_clients:
            if self.userinfo_uri:
                # An empty allowlist is NOT a kill switch when userinfo is
                # configured: opaque tokens validated via the userinfo fallback
                # bypass ALLOWED_MGMT_CLIENT (per-user authz still applies).
                logger.warning(
                    "ALLOWED_MGMT_CLIENT is unset: JWT/introspection management "
                    "tokens will be rejected, but opaque tokens may still be "
                    "accepted via the userinfo fallback."
                )
            else:
                logger.warning(
                    "ALLOWED_MGMT_CLIENT is unset or empty: management API will "
                    "reject all requests until configured."
                )
        else:
            logger.info(
                "Management API allowlist: %s", sorted(self._allowed_mgmt_clients)
            )

        logger.info(
            "UnifiedTokenVerifier initialized (multi-audience). MCP audience: %s or %s, Nextcloud resource URI: %s, Valid issuers: %s",
            settings.oidc_client_id,
            settings.nextcloud_mcp_server_url,
            settings.nextcloud_resource_uri,
            self.valid_issuers,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Verify token according to MCP TokenVerifier protocol.

        Per RFC 7519, we validate only MCP audience. The token is then used
        directly against Nextcloud (which validates its own audience) — see
        context_helper.py.

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

        return await self._verify_mcp_audience(token)

    async def verify_token_for_management_api(self, token: str) -> AccessToken | None:
        """
        Verify token for management API access (ADR-018 NC PHP app integration).

        This verification accepts ANY valid Nextcloud OIDC token, not just tokens
        with MCP server audience. This is needed because:
        - Astrolabe (NC PHP app) uses its own OAuth client with Nextcloud OIDC
        - Tokens from Astrolabe have Astrolabe's client_id as audience
        - MCP server's management API should accept these tokens

        Security Model:
        ~~~~~~~~~~~~~~~~
        This relaxed audience validation is secure because:

        1. **Authentication layer** (this method):
           - Verifies token signature against Nextcloud's JWKS (cryptographic proof)
           - Verifies token is not expired
           - Extracts user identity from validated token claims
           - NOTE: for opaque cross-client tokens (e.g. Astrolabe) that
             introspection reports inactive, authentication falls back to the
             userinfo endpoint — a live IdP liveness check (200 + ``sub``)
             rather than local JWKS/expiry verification. Such tokens are stamped
             ``_auth_via_userinfo`` and bypass the client allowlist (step 4);
             per-user authorization (step 2) remains the security gate.

        2. **Authorization layer** (management API endpoints):
           - EVERY endpoint verifies: token.sub == requested_resource_owner
           - Example: GET /users/{user_id}/session checks token_user_id == path_user_id
           - Users can ONLY access their own resources, never another user's

        3. **Attack scenario analysis**:
           - Attacker with stolen token for App A cannot access user B's data
           - Token's `sub` claim is cryptographically bound to a specific user
           - Authorization layer rejects cross-user access attempts (403 Forbidden)

        4. **Why audience validation isn't needed here**:
           - Audience validation prevents token confusion attacks across services
           - But management API authorization already gates access per-user
           - A token valid for "astrolabe" is still bound to user X, not user Y

        Args:
            token: Bearer token to verify

        Returns:
            AccessToken if valid AND issued by an allowlisted client, None otherwise
        """
        # Check cache first (using separate cache key to avoid mixing with MCP tokens)
        cache_key = f"mgmt:{hashlib.sha256(token.encode()).hexdigest()}"
        access_token: AccessToken | None = None
        if cache_key in self._token_cache:
            userinfo, expiry = self._token_cache[cache_key]
            if time.time() < expiry:
                logger.debug("Management API token found in cache")
                oauth_token_cache_hits_total.labels(hit="true").inc()
                username = userinfo.get("sub") or userinfo.get("preferred_username")
                scope_string = userinfo.get("scope", "")
                scopes = scope_string.split() if scope_string else []
                access_token = AccessToken(
                    token=token,
                    client_id=userinfo.get("client_id", ""),
                    scopes=scopes,
                    expires_at=int(expiry),
                    resource=username,
                )
            else:
                del self._token_cache[cache_key]

        from_cache = access_token is not None
        if access_token is None:
            oauth_token_cache_hits_total.labels(hit="false").inc()
            access_token = await self._verify_without_audience_check(token, cache_key)

        if access_token is None:
            return None

        # Opaque tokens validated via the userinfo fallback carry no verifiable
        # client_id, so the ALLOWED_MGMT_CLIENT allowlist cannot apply. Such
        # tokens are stamped with ``_auth_via_userinfo`` in the cache; for them
        # we rely on the per-user authorization every management endpoint
        # enforces (token sub == requested resource owner).
        # Recover the via-userinfo flag from the cache entry. On a cache miss
        # this is the entry _verify_without_audience_check just wrote (no await
        # between that write and this read, so it is always present); on a cache
        # hit it was written by an earlier call.
        cached_entry = self._token_cache.get(cache_key)
        via_userinfo = bool(cached_entry and cached_entry[0].get("_auth_via_userinfo"))
        if via_userinfo:
            # Warn once on fresh validation; subsequent cache-hit re-validations
            # (frequent Astrolabe polling) log at DEBUG to avoid flooding.
            if from_cache:
                logger.debug(
                    "Opaque token (userinfo-validated) served from cache for "
                    "user %s; allowlist not enforced",
                    access_token.resource,
                )
            else:
                logger.warning(
                    "Opaque token validated via userinfo endpoint; "
                    "ALLOWED_MGMT_CLIENT allowlist not enforced for user %s "
                    "(per-user authorization applies)",
                    access_token.resource,
                )
            return access_token

        # Enforce ALLOWED_MGMT_CLIENT allowlist (fail-closed when unset)
        token_client_id = access_token.client_id
        if not token_client_id or token_client_id not in self._allowed_mgmt_clients:
            logger.warning(
                "Management API token rejected: client_id %r not in ALLOWED_MGMT_CLIENT",
                token_client_id,
            )
            return None

        return access_token

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
                    # Fall back to introspection if JWT verification failed
                    if self.introspection_uri:
                        validation_method = "introspect"
                        payload = await self._introspect_token(token)
                        if payload:
                            record_oauth_token_validation("introspect", "valid")
                        else:
                            record_oauth_token_validation("introspect", "invalid")
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
                    "Token rejected: Missing MCP audience. Got %s, need MCP (%s or %s)",
                    audiences,
                    self.settings.oidc_client_id,
                    self.settings.nextcloud_mcp_server_url,
                )
                # Record as invalid due to audience mismatch
                record_oauth_token_validation(validation_method, "invalid")
                return None

            logger.info(
                "MCP audience validated - token can be used directly "
                "(Nextcloud will validate its own audience)"
            )

            return self._create_access_token(token, payload)

        except Exception as e:
            logger.error("Token verification failed: %s", e)
            record_oauth_token_validation(validation_method, "error")
            return None

    async def _verify_without_audience_check(
        self, token: str, cache_key: str
    ) -> AccessToken | None:
        """
        Verify token validity without checking MCP audience or issuer.

        Used for management API where tokens from Astrolabe (NC PHP app) need to
        be accepted. These tokens are issued by Nextcloud OIDC to Astrolabe's
        OAuth client, not MCP server's client.

        What we verify:
        - ✓ Token signature (cryptographic proof token is from Nextcloud OIDC)
        - ✓ Token expiration (not expired)
        - ✓ Token structure (valid JWT format)

        What we skip:
        - ✗ Audience check (token may have Astrolabe's audience, not MCP's)
        - ✗ Issuer check (token may have internal Nextcloud URL as issuer)

        Security guarantee:
        - Authorization is enforced by management API endpoints
        - Each endpoint verifies: token.sub == requested_resource_owner
        - See verify_token_for_management_api() docstring for full security model

        Args:
            token: Bearer token to verify
            cache_key: Cache key for storing validation result

        Returns:
            AccessToken if valid, None otherwise
        """
        validation_method = "unknown"
        try:
            # Attempt JWT verification first
            # Skip issuer check for management API tokens (may have internal URL)
            if self._is_jwt_format(token) and self.jwks_client:
                validation_method = "jwt"
                payload = await self._verify_jwt_signature(
                    token, skip_issuer_check=True
                )
                if payload:
                    record_oauth_token_validation("jwt", "valid")
                else:
                    record_oauth_token_validation("jwt", "invalid")
                    return None
            else:
                # Opaque token: try introspection first (only when configured),
                # then fall back to userinfo. userinfo validates opaque tokens
                # minted for a *different* OIDC client (e.g. Astrolabe) that
                # introspection reports inactive cross-client.
                payload = None
                if self.introspection_uri:
                    validation_method = "introspect"
                    payload = await self._introspect_token(token)
                    if payload:
                        record_oauth_token_validation("introspect", "valid")
                    else:
                        record_oauth_token_validation("introspect", "invalid")

                # Fall through to userinfo when introspection is unconfigured or
                # returned None. NOTE: _introspect_token returns None for BOTH an
                # active=false response (the nx101294 cross-client case we must
                # handle) AND a network/timeout error — both reach userinfo here.
                # That is safe: userinfo is itself an authoritative live check (a
                # revoked/invalid token gets a 401), so a flapping introspection
                # endpoint cannot cause an invalid token to be accepted.
                if payload is None and self.userinfo_uri:
                    # Set validation_method first so a userinfo exception caught
                    # by the outer handler is attributed correctly.
                    validation_method = "userinfo"
                    payload = await self._validate_via_userinfo(token)
                    if payload:
                        record_oauth_token_validation("userinfo", "valid")
                    else:
                        record_oauth_token_validation("userinfo", "invalid")
                        return None

                if payload is None:
                    # No validator was configured, or none succeeded. Don't record
                    # a userinfo failure metric when userinfo was never attempted.
                    return None

            # Both branches above either set a populated payload or have already
            # returned None, so payload is guaranteed truthy here.

            # Skip audience validation - any valid Nextcloud token is accepted
            logger.debug(
                "Management API token validated (no audience check) for user: %s",
                payload.get("sub"),
            )

            # Cache and return the token. via_userinfo is derived from how we
            # actually validated — never from a payload claim (see
            # _create_access_token_with_cache_key).
            return self._create_access_token_with_cache_key(
                token,
                payload,
                cache_key,
                via_userinfo=(validation_method == "userinfo"),
            )

        except Exception as e:
            logger.error("Management API token verification failed: %s", e)
            record_oauth_token_validation(validation_method, "error")
            return None

    def _has_mcp_audience(self, payload: dict[str, Any]) -> bool:
        """
        Check if token has MCP audience.

        Per RFC 7519 Section 4.1.3, resource servers should only validate their own
        presence in the audience claim. We don't validate Nextcloud's audience - that's
        Nextcloud's responsibility when it receives the token.

        AWS Cognito access tokens do not include an ``aud`` claim — they use
        ``client_id`` instead.  When ``aud`` is absent we fall back to
        ``client_id`` so that Cognito-issued tokens are accepted.

        Args:
            payload: Decoded token payload

        Returns:
            True if MCP audience present, False otherwise
        """
        audiences = payload.get("aud", [])
        if isinstance(audiences, str):
            audiences = [audiences]

        audiences_set = set(audiences)

        # Cognito fallback: access tokens carry client_id instead of aud
        if not audiences_set:
            token_client_id = payload.get("client_id", "")
            if token_client_id:
                audiences_set = {token_client_id}

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

    async def _verify_jwt_signature(
        self, token: str, skip_issuer_check: bool = False
    ) -> dict[str, Any] | None:
        """
        Verify JWT token with signature validation using JWKS.

        Args:
            token: JWT token to verify
            skip_issuer_check: If True, skip issuer validation (for management API tokens)

        Returns:
            Decoded payload if valid, None if invalid
        """
        try:
            assert self.jwks_client is not None  # Caller should check before calling

            # Get signing key from JWKS
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)

            # Verify and decode JWT
            # Note: We don't validate audience here - that's done separately based on mode
            # Issuer is checked manually below to support multiple valid issuers
            # (internal Docker URL vs public URL in AS proxy deployments)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": False,  # Checked manually below
                    "verify_aud": False,  # Handled separately based on mode
                },
            )

            # Manual issuer validation against multiple valid issuers
            if not skip_issuer_check and self.valid_issuers:
                token_issuer = payload.get("iss")
                if token_issuer not in self.valid_issuers:
                    raise jwt.InvalidIssuerError(
                        f"Invalid issuer '{token_issuer}', "
                        f"expected one of: {self.valid_issuers}"
                    )

            logger.debug("JWT signature verified for user: %s", payload.get("sub"))
            return payload

        except jwt.ExpiredSignatureError:
            logger.info("JWT token has expired")
            return None
        except jwt.InvalidIssuerError as e:
            logger.warning("JWT issuer validation failed: %s", e)
            return None
        except jwt.InvalidTokenError as e:
            logger.warning("JWT validation failed: %s", e)
            return None
        except Exception as e:
            logger.error("Unexpected error during JWT verification: %s", e)
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
            client_id = self.settings.oidc_client_id
            client_secret = self.settings.oidc_client_secret
            assert client_id is not None and client_secret is not None
            response = await self.http_client.post(
                self.introspection_uri,
                data={"token": token},
                auth=(client_id, client_secret),
            )

            if response.status_code == 200:
                introspection_data = response.json()

                # Check if token is active
                if not introspection_data.get("active", False):
                    logger.info("Token introspection returned inactive=false")
                    return None

                logger.debug(
                    "Token introspected successfully for user: %s",
                    introspection_data.get("sub"),
                )
                return introspection_data

            elif response.status_code in (400, 401, 403):
                logger.warning(
                    "Token introspection failed: HTTP %s. Response: %s",
                    response.status_code,
                    response.text[:200] if response.text else "empty",
                )
                return None
            else:
                logger.warning(
                    "Unexpected response from introspection: %s. Response: %s",
                    response.status_code,
                    response.text[:200] if response.text else "empty",
                )
                return None

        except httpx.TimeoutException:
            logger.error("Timeout while introspecting token")
            return None
        except httpx.RequestError as e:
            logger.error("Network error while introspecting token: %s", e)
            return None
        except Exception as e:
            logger.error("Unexpected error during token introspection: %s", e)
            return None

    async def _validate_via_userinfo(self, token: str) -> dict[str, Any] | None:
        """Validate an opaque token by calling the OIDC userinfo endpoint.

        Fallback for opaque access tokens that the Nextcloud ``oidc`` app's
        introspection endpoint reports ``active=false`` cross-client (i.e.
        tokens minted for a *different* client such as Astrolabe). A 200 from
        userinfo with a ``sub`` claim proves the bearer is a valid, unexpired
        token for that user.

        Unlike introspection, userinfo returns neither ``client_id`` nor
        ``scope``. The caller signals this path via the ``via_userinfo`` argument
        to :meth:`_create_access_token_with_cache_key` (never inferred from a
        payload claim, so a malicious IdP response cannot forge it), and the
        management-API allowlist is relaxed for it (authorization is still
        enforced per-user by every management endpoint).

        Caution: userinfo-validated tokens carry **empty scopes**. Callers must
        not gate management endpoints on scopes for this path (e.g. a future
        ``@require_scopes``) or they would silently reject valid cross-client
        tokens; the per-user ``sub`` check is the authorization gate.

        Security note — bounded staleness: userinfo carries no token ``exp``, so
        a validated token is cached for ``userinfo_cache_ttl`` (5 min) rather
        than the 1-hour default. A revoked/expired opaque token may therefore be
        honored from cache for up to that window before re-validation.

        Args:
            token: Bearer token to validate.

        Returns:
            Userinfo claims if valid, else None.
        """
        # Defensive: the management-API caller already gates on
        # self.userinfo_uri before invoking this, but the guard keeps the method
        # safe to call directly (e.g. in unit tests).
        if not self.userinfo_uri:
            logger.debug("No userinfo endpoint configured")
            return None

        # userinfo_uri comes from the OIDC discovery document (admin-configured),
        # not from user input — but guard the scheme anyway to satisfy SSRF
        # scanners and to fail fast on a misconfigured endpoint.
        if not self.userinfo_uri.startswith(("https://", "http://")):
            logger.error("Refusing non-HTTP userinfo_uri: %s", self.userinfo_uri)
            return None

        try:
            response = await self.http_client.get(
                self.userinfo_uri,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException:
            logger.error("Timeout while validating token via userinfo")
            return None
        except httpx.RequestError as e:
            logger.error("Network error while validating token via userinfo: %s", e)
            return None

        if response.status_code != 200:
            logger.warning(
                "Userinfo token validation failed: HTTP %s", response.status_code
            )
            return None

        try:
            data = response.json()
        except Exception as e:
            logger.error("Failed to parse userinfo response: %s", e)
            return None

        if not data.get("sub"):
            logger.warning("Userinfo response missing 'sub' claim")
            return None

        logger.debug("Token validated via userinfo for user: %s", data.get("sub"))
        return data

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
        # Use default cache key (hash of token)
        cache_key = hashlib.sha256(token.encode()).hexdigest()
        return self._create_access_token_with_cache_key(token, payload, cache_key)

    def _create_access_token_with_cache_key(
        self,
        token: str,
        payload: dict[str, Any],
        cache_key: str,
        *,
        via_userinfo: bool = False,
    ) -> AccessToken | None:
        """
        Create AccessToken object from validated token payload with custom cache key.

        Args:
            token: The bearer token
            payload: Validated token payload
            cache_key: Key to use for caching (allows separate caches for MCP vs management API)
            via_userinfo: True when the token was validated via the userinfo
                fallback. Sourced from the caller (how validation happened), never
                from a payload claim — it gates the allowlist relaxation and the
                short cache TTL, so it must not be forgeable by the IdP response.

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
            "Extracted scopes from token - scope claim: '%s' -> scopes list: %s",
            scope_string,
            scopes,
        )

        # Extract expiration
        exp = payload.get("exp")
        if not exp:
            # userinfo-validated tokens never carry exp (userinfo describes the
            # user, not the token). Cache them only briefly so a revoked/expired
            # opaque token can't be honored for the full hour-long default TTL.
            if via_userinfo:
                ttl = self.userinfo_cache_ttl
                # userinfo never returns exp, so this fires on every fresh
                # userinfo validation — keep it at DEBUG (the bounded-staleness
                # window is documented on _validate_via_userinfo).
                logger.debug(
                    "Token validated via userinfo has no 'exp'; caching for %ss only",
                    ttl,
                )
            else:
                ttl = self.cache_ttl
                logger.warning("No 'exp' claim in token, using default TTL")
            exp = int(time.time() + ttl)

        # Cache the result with the provided key. Drop any `_auth_via_userinfo`
        # carried in the IdP payload — that flag is the allowlist-bypass signal
        # and must originate ONLY from the trusted in-process `via_userinfo`
        # argument, never from a (potentially malicious) introspection/userinfo
        # claim.
        userinfo = {
            "sub": username,
            "scope": scope_string,
            **{
                k: v
                for k, v in payload.items()
                if k not in ("sub", "scope", "_auth_via_userinfo")
            },
        }
        if via_userinfo:
            userinfo["_auth_via_userinfo"] = True
        self._token_cache[cache_key] = (userinfo, exp)

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
