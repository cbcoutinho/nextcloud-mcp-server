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

import base64
import hashlib
import json
import logging
import time
from collections.abc import Mapping
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
                    client_id=self._client_id_from_claims(userinfo),
                    scopes=scopes,
                    expires_at=int(expiry),
                    resource=username,
                )
            else:
                del self._token_cache[cache_key]

        from_cache = access_token is not None
        outcome: dict[str, str] = {}
        if access_token is None:
            oauth_token_cache_hits_total.labels(hit="false").inc()
            access_token = await self._verify_without_audience_check(
                token, cache_key, outcome
            )

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
            self._record_mgmt_grant(outcome, from_cache, access_token.client_id)
            return access_token

        # Enforce ALLOWED_MGMT_CLIENT allowlist (fail-closed when unset)
        token_client_id = access_token.client_id
        if not token_client_id or token_client_id not in self._allowed_mgmt_clients:
            # Authorization, not validation — the token is genuine, its client
            # is simply not on the management allowlist. Recorded through the
            # same funnel anyway: from the operator's side this is
            # indistinguishable from any other reason a client stopped working,
            # and answering "why was it disconnected?" is the point.
            #
            # An *empty* allowlist is a different failure from a client missing
            # off a populated one: it rejects every client, and it is our
            # misconfiguration rather than the caller's token. Same shape as the
            # JWKS/introspection `not_configured` cases, so it belongs on the
            # same pageable side of the result split.
            unconfigured = not self._allowed_mgmt_clients
            return self._reject(
                "allowlist",
                "not_configured" if unconfigured else "not_allowlisted",
                token,
                "ALLOWED_MGMT_CLIENT is unset — every management client is rejected"
                if unconfigured
                else f"client_id {token_client_id!r} not in ALLOWED_MGMT_CLIENT",
                # Already verified — the token validated, it is only the
                # authorization that failed. Re-deriving from the raw bytes
                # would lose it entirely for opaque tokens.
                client_id=token_client_id,
            )

        self._record_mgmt_grant(outcome, from_cache, access_token.client_id)
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
        # Stage failures accumulate here; the chain is recorded once, at the
        # end, so asking two validators about one token still produces one
        # rejection. See _note() / _reject_chain().
        chain: list[tuple[str, str, str | None]] = []
        try:
            # Attempt JWT verification first
            if self._is_jwt_format(token) and self.jwks_client:
                validation_method = "jwt"
                payload = await self._verify_jwt_signature(token, chain=chain)
                if not payload and self.introspection_uri:
                    # Fall back to introspection if JWT verification failed
                    validation_method = "introspect"
                    payload = await self._introspect_token(token, chain=chain)
            else:
                # Fall back to introspection for opaque tokens
                validation_method = "introspect"
                payload = await self._introspect_token(token, chain=chain)

            # Check payload is valid
            if not payload:
                return self._reject_chain(chain, token)

            # Validate MCP audience is present
            if not self._has_mcp_audience(payload):
                audiences = payload.get("aud", [])
                return self._reject(
                    validation_method,
                    "bad_audience",
                    token,
                    f"got {audiences}, need {self.settings.oidc_client_id} "
                    f"or {self.settings.nextcloud_mcp_server_url}",
                )

            logger.info(
                "MCP audience validated - token can be used directly "
                "(Nextcloud will validate its own audience)"
            )

            # Resolve the raw identity claim to Nextcloud's own account UID
            # before it becomes AccessToken.resource (#1326): everything
            # downstream keys storage on that field.
            raw_username = payload.get("sub") or payload.get("preferred_username")
            resolved_username = (
                await self._resolve_canonical_uid(token, raw_username)
                if raw_username
                else None
            )

            # Recorded only once the AccessToken actually exists — not at each
            # validation stage, and not merely once the audience check passes.
            # `_create_access_token` still returns None (without raising) for a
            # payload carrying no `sub`/`preferred_username`, so recording any
            # earlier means a token that is about to be refused is counted as
            # accepted. "valid" has to mean the caller got a token.
            access_token = self._create_access_token(
                token, payload, resolved_username=resolved_username
            )
            if access_token is None:
                return self._reject(
                    validation_method,
                    "unknown",
                    token,
                    "no 'sub' or 'preferred_username' claim in token payload",
                )
            # client_id from the AccessToken, not omitted: without it every
            # `valid` record labels `client_id="unknown"`, so the metric can say
            # why a client was rejected but not which client succeeded — and a
            # rejection rate needs both halves to mean anything.
            record_oauth_token_validation(
                validation_method, "valid", client_id=access_token.client_id
            )
            return access_token

        except Exception as e:
            return self._reject(validation_method, "unknown", token, str(e))

    async def _verify_without_audience_check(
        self,
        token: str,
        cache_key: str,
        outcome: dict[str, str] | None = None,
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
        # See _verify_mcp_audience: one rejection per token, recorded terminally.
        chain: list[tuple[str, str, str | None]] = []
        try:
            # Attempt JWT verification first
            # Skip issuer check for management API tokens (may have internal URL)
            if self._is_jwt_format(token) and self.jwks_client:
                validation_method = "jwt"
                payload = await self._verify_jwt_signature(
                    token, skip_issuer_check=True, chain=chain
                )
                if not payload:
                    return self._reject_chain(chain, token)
            else:
                # Opaque token: try introspection first (only when configured),
                # then fall back to userinfo. userinfo validates opaque tokens
                # minted for a *different* OIDC client (e.g. Astrolabe) that
                # introspection reports inactive cross-client.
                payload = None
                if self.introspection_uri:
                    validation_method = "introspect"
                    payload = await self._introspect_token(token, chain=chain)

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
                    payload = await self._validate_via_userinfo(token, chain=chain)
                    if not payload:
                        return self._reject_chain(chain, token)

                if payload is None:
                    if not self.introspection_uri and not self.userinfo_uri:
                        # Nothing was even attempted: an opaque token arrived and
                        # this server has no way to validate one. That is the
                        # management-API twin of the quiet `jwks_client is None`
                        # branch this PR started from, and it was quieter still —
                        # not DEBUG, nothing at all.
                        return self._reject("unknown", "not_configured", token)
                    return self._reject_chain(chain, token)

            # Both branches above either set a populated payload or have already
            # returned None, so payload is guaranteed truthy here.

            # Skip audience validation - any valid Nextcloud token is accepted
            logger.debug(
                "Management API token validated (no audience check) for user: %s",
                payload.get("sub"),
            )

            # Resolve the raw identity claim to Nextcloud's own account UID
            # before it becomes AccessToken.resource (#1326), same as the
            # MCP-audience path.
            raw_username = payload.get("sub") or payload.get("preferred_username")
            resolved_username = (
                await self._resolve_canonical_uid(token, raw_username)
                if raw_username
                else None
            )

            # Cache and return the token. via_userinfo is derived from how we
            # actually validated — never from a payload claim (see
            # _create_access_token_with_cache_key).
            access_token = self._create_access_token_with_cache_key(
                token,
                payload,
                cache_key,
                via_userinfo=(validation_method == "userinfo"),
                resolved_username=resolved_username,
            )
            # Creation still returns None (without raising) for a payload with
            # no `sub`/`preferred_username`, and a stage passing is not the same
            # thing as the caller getting a token.
            if access_token is None:
                return self._reject(
                    validation_method,
                    "unknown",
                    token,
                    "no 'sub' or 'preferred_username' claim in token payload",
                )
            # NOT recorded as `valid` here. This method's caller still has the
            # ALLOWED_MGMT_CLIENT decision to make, and a token refused there is
            # not a token that was served. Recording at this point counted a
            # rejected request as both accepted and rejected. The caller reports
            # which validator succeeded via `outcome` and records once, at the
            # point the request is actually granted.
            if outcome is not None:
                outcome["method"] = validation_method
            return access_token

        except Exception as e:
            return self._reject(validation_method, "unknown", token, str(e))

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

    @staticmethod
    def _client_id_from_claims(claims: Mapping[str, Any]) -> str:
        """The client identity of a *verified* payload.

        Two spellings are in the wild and an IdP generally stamps only one.
        RFC 9068 (JWT access tokens) puts it in ``client_id`` — that is what the
        Nextcloud ``oidc`` app emits. Keycloak, Authentik and the rest of the
        OIDC-Core lineage use ``azp`` (authorized party) instead and never emit
        ``client_id`` at all.

        Reading only ``client_id`` therefore made every token from an external
        IdP look like it had no client, so ``ALLOWED_MGMT_CLIENT`` refused it no
        matter how it was configured (cbcoutinho/astrolabe#324).

        ``aud`` is deliberately NOT consulted here, unlike in
        :meth:`_claimed_client_id`, which only feeds a log line. The audience
        says who a token is *for*, not who asked for it; treating it as an
        identity would let any token minted for an allowlisted audience pass the
        allowlist regardless of which client obtained it.

        Returns "" when neither claim is a non-empty string — the same
        fail-closed value the allowlist already rejects.
        """
        for key in ("client_id", "azp"):
            value = claims.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _claimed_client_id(token: str) -> str | None:
        """Read the client_id a token *claims*, without verifying anything.

        A rejected token has by definition not been validated, so its contents
        are untrusted — but they are also the only thing that says which client
        was turned away, which is exactly what an operator needs to know. The
        value is treated as untrusted downstream (clamped and count-bounded
        before it becomes a metric label) rather than being thrown away.

        The payload segment is base64-decoded directly rather than going through
        ``jwt.decode(..., verify_signature=False)``. Same result, but it does not
        route untrusted input through the JWT library at all, so there is no
        chance of the value being mistaken for an authenticated claim by a later
        reader (or by a security scanner — python:S5659 flags the library call
        regardless of intent, and suppressing it would hide the real thing).

        Returns None for opaque tokens and anything unparseable.
        """
        parts = token.split(".")
        if len(parts) != 3:
            return None  # opaque token, not a JWS
        try:
            # Restore the padding base64url encoding strips.
            payload = parts[1]
            claims = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
        except Exception:
            return None
        if not isinstance(claims, dict):
            return None
        for key in ("client_id", "azp", "aud"):
            value = claims.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, list) and value and isinstance(value[0], str):
                return value[0]
        return None

    # Whose problem is it? A rejection is either the caller presenting a token
    # we cannot accept, or this server being unable to validate one. Those want
    # different responses — the second should page — so `result` is derived from
    # `reason` here rather than passed in at each call site. It was a parameter
    # defaulting to "invalid", and 4 of the 6 `not_configured` sites silently
    # took the default, which put the single most total failure mode in the
    # system (no validator configured at all) on the *client's* side of the
    # split, contradicting the documented contract.
    _OUR_FAULT_REASONS = frozenset(
        {"not_configured", "network_error", "no_signing_keys", "unknown"}
    )

    def _reject(
        self,
        method: str,
        reason: str,
        token: str,
        detail: str | None = None,
        client_id: str | None = None,
    ) -> None:
        """Record and log a token rejection, then return None for the caller.

        Every rejection path funnels through here so the *reason* survives.
        Previously each path logged its own line at its own level (several at
        DEBUG, invisible in production) and reported a bare "invalid" to the
        metric — so a client whose users were being forced to re-authenticate
        produced a counter that said only "something was rejected".

        WARNING, not INFO: a rejection ends an MCP client's session and makes a
        human log in again. That is not routine.

        Args:
            client_id: Pass this when the caller already holds a *verified*
                client id, i.e. the token validated and was then rejected on
                authorization grounds. The fallback below can only read a JWT,
                so an opaque token would otherwise be recorded as "unknown"
                despite its identity being known — and clients may present
                either token type, so that is not a rare shape.

                An *empty* value falls back too, deliberately. A verified
                payload can carry `azp`/`aud` without a top-level `client_id`
                (`_create_access_token_with_cache_key` reads only the latter,
                defaulting to ""), and for a JWT the fallback re-reads those
                same already-verified bytes. Preferring "" over a recoverable
                identity would lose information for no gain.
        """
        client_id = client_id or self._claimed_client_id(token)
        result = "error" if reason in self._OUR_FAULT_REASONS else "invalid"
        record_oauth_token_validation(method, result, reason, client_id)
        logger.warning(
            "Token rejected (%s/%s, %s) for client %s%s",
            method,
            reason,
            "our fault" if result == "error" else "caller's token",
            client_id or "unknown",
            f": {detail}" if detail else "",
        )
        return None

    def _record_mgmt_grant(
        self, outcome: dict[str, str], from_cache: bool, client_id: str | None = None
    ) -> None:
        """Record a management-API request as accepted, once it actually is.

        Authentication succeeding is not the same as the request being granted:
        ALLOWED_MGMT_CLIENT can still refuse a perfectly valid token. Recording
        `valid` when the token verified counted a refused request as both
        served and rejected, which is the same "record at the point the
        decision was actually made" invariant applied one method further out.

        Cache hits are skipped so this keeps counting *validations*, matching
        the existing behaviour where a cached token records no validation at
        all (``mcp_oauth_token_cache_hits_total`` covers that side).
        """
        if from_cache:
            return
        record_oauth_token_validation(
            outcome.get("method", "unknown"), "valid", client_id=client_id
        )

    def _note(
        self,
        chain: list[tuple[str, str, str | None]] | None,
        method: str,
        reason: str,
        token: str,
        detail: str | None = None,
    ) -> None:
        """Record a rejection, or defer it when a fallback may still succeed.

        A verifier that falls back — JWT then introspection, introspection then
        userinfo — asks two validators about one token. Each failing stage used
        to record and log independently, so a single expired token produced two
        metric increments and two WARNINGs. That is the *same* "two breadcrumbs
        to correlate" problem this work set out to remove, rebuilt one layer up
        and made louder by promoting the lines to WARNING.

        When ``chain`` is provided the stage failure is appended to it and the
        caller records once, terminally, via :meth:`_reject_chain`. When it is
        None — a direct call, including from tests — the rejection is recorded
        immediately, since there is no chain to be the tail of.
        """
        if chain is None:
            return self._reject(method, reason, token, detail)
        chain.append((method, reason, detail))
        return None

    def _reject_chain(
        self, chain: list[tuple[str, str, str | None]], token: str
    ) -> None:
        """Record a fallback chain's outcome as the single rejection it is.

        Attributed to the **last** validator tried, because that is the one
        whose refusal actually produced the 401. Earlier stages are folded into
        the detail so the whole story stays on one line — losing "the JWT was
        expired *and then* introspection said inactive" would trade one problem
        for another.
        """
        if not chain:
            return None
        method, reason, detail = chain[-1]
        if len(chain) > 1:
            preceding = " → ".join(f"{m}/{r}" for m, r, _ in chain[:-1])
            detail = f"after {preceding}" + (f": {detail}" if detail else "")
        return self._reject(method, reason, token, detail)

    async def _verify_jwt_signature(
        self,
        token: str,
        skip_issuer_check: bool = False,
        chain: list[tuple[str, str, str | None]] | None = None,
    ) -> dict[str, Any] | None:
        """
        Verify JWT token with signature validation using JWKS.

        Args:
            token: JWT token to verify
            skip_issuer_check: If True, skip issuer validation (for management API tokens)

        Returns:
            Decoded payload if valid, None if invalid
        """
        if self.jwks_client is None:
            # Callers are expected to check first; returning None keeps that
            # contract without an assert inside the try/except below, which
            # would have swallowed the AssertionError and reported a
            # configuration bug as an invalid token (python:S5779).
            # Was DEBUG, i.e. invisible at the production log level: a missing
            # JWKS client rejects every JWT, so the one condition that breaks
            # all clients at once was the quietest thing in here.
            return self._note(chain, "jwt", "not_configured", token)

        try:
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
            return self._note(chain, "jwt", "expired", token)
        except jwt.InvalidIssuerError as e:
            return self._note(chain, "jwt", "bad_issuer", token, str(e))
        except jwt.InvalidTokenError as e:
            # Covers signature failures, malformed tokens and bad `iat`.
            return self._note(chain, "jwt", "bad_signature", token, str(e))
        except jwt.PyJWKSetError as e:
            # The JWKS was fetched and parsed fine but carries no usable signing
            # key, so every JWT is unverifiable. That is an IdP/operator
            # misconfiguration, not an outage and not a bad token — most often
            # a provider left on symmetric (HS256) signing, which serves an
            # empty key set: Authentik and Keycloak both publish JWKS keys only
            # for asymmetric signing keys.
            #
            # Separate from PyJWKClientError below because PyJWKSetError is NOT
            # a subclass of it (both derive straight from PyJWTError), so it
            # otherwise fell through to the generic handler and surfaced as
            # "unknown" — indistinguishable from a real internal bug, and the
            # operator got a bare 401 with the cause only in our logs.
            return self._note(
                chain,
                "jwt",
                "no_signing_keys",
                token,
                f"{e} (JWKS: {getattr(self.jwks_client, 'uri', 'unknown')})",
            )
        except jwt.PyJWKClientError as e:
            # Fetching the signing key failed — the JWKS endpoint is down, slow
            # or unreachable. Nothing is wrong with the caller's token. This is
            # NOT an InvalidTokenError subclass, so without an explicit clause
            # it lands in the generic handler as "unknown", and a JWKS outage
            # reads differently from the introspection and userinfo outages it
            # is the exact peer of.
            return self._note(chain, "jwt", "network_error", token, str(e))
        except Exception as e:
            return self._note(chain, "jwt", "unknown", token, str(e))

    async def _introspect_token(
        self, token: str, chain: list[tuple[str, str, str | None]] | None = None
    ) -> dict[str, Any] | None:
        """
        Validate token by calling the introspection endpoint (RFC 7662).

        Args:
            token: Bearer token to introspect

        Returns:
            Token payload if active, None if inactive or invalid
        """
        if not self.introspection_uri:
            return self._note(chain, "introspect", "not_configured", token)

        # Introspection requires client authentication. Checked before the try
        # rather than asserted inside it: the except below would have caught
        # the AssertionError and logged missing configuration as a failed
        # introspection (python:S5779).
        client_id = self.settings.oidc_client_id
        client_secret = self.settings.oidc_client_secret
        if client_id is None or client_secret is None:
            return self._note(
                chain,
                "introspect",
                "not_configured",
                token,
                "introspection endpoint set but OIDC client credentials are not",
            )

        try:
            response = await self.http_client.post(
                self.introspection_uri,
                data={"token": token},
                auth=(client_id, client_secret),
            )

            if response.status_code == 200:
                introspection_data = response.json()

                # Check if token is active
                if not introspection_data.get("active", False):
                    return self._note(chain, "introspect", "inactive", token)

                logger.debug(
                    "Token introspected successfully for user: %s",
                    introspection_data.get("sub"),
                )
                return introspection_data

            else:
                # 400/401/403 mean the *server's* introspection credentials are
                # wrong, not the caller's token; anything else is unexpected.
                # Both are our problem, not the client's.
                return self._note(
                    chain,
                    "introspect",
                    "not_configured"
                    if response.status_code in (400, 401, 403)
                    else "unknown",
                    token,
                    f"HTTP {response.status_code}: "
                    f"{response.text[:200] if response.text else 'empty'}",
                )

        except httpx.TimeoutException:
            return self._note(chain, "introspect", "network_error", token, "timeout")
        except httpx.RequestError as e:
            return self._note(chain, "introspect", "network_error", token, str(e))
        except Exception as e:
            return self._note(chain, "introspect", "unknown", token, str(e))

    async def _validate_via_userinfo(
        self, token: str, chain: list[tuple[str, str, str | None]] | None = None
    ) -> dict[str, Any] | None:
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
            return self._note(chain, "userinfo", "not_configured", token)

        # userinfo_uri comes from the OIDC discovery document (admin-configured),
        # not from user input — but guard the scheme anyway to satisfy SSRF
        # scanners and to fail fast on a misconfigured endpoint.
        if not self.userinfo_uri.startswith(("https://", "http://")):
            return self._note(
                chain,
                "userinfo",
                "not_configured",
                token,
                f"refusing non-HTTP userinfo_uri: {self.userinfo_uri}",
            )

        try:
            response = await self.http_client.get(
                self.userinfo_uri,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException:
            return self._note(chain, "userinfo", "network_error", token, "timeout")
        except httpx.RequestError as e:
            return self._note(chain, "userinfo", "network_error", token, str(e))

        if response.status_code != 200:
            # userinfo answers 401 for an expired or revoked opaque token; that
            # is the caller's token being bad, not our configuration.
            return self._note(
                chain,
                "userinfo",
                "inactive" if response.status_code == 401 else "unknown",
                token,
                f"HTTP {response.status_code}",
            )

        try:
            data = response.json()
        except Exception as e:
            return self._note(
                chain, "userinfo", "unknown", token, f"unparseable response: {e}"
            )

        if not data.get("sub"):
            return self._note(
                chain, "userinfo", "unknown", token, "response missing 'sub' claim"
            )

        logger.debug("Token validated via userinfo for user: %s", data.get("sub"))
        return data

    async def _resolve_canonical_uid(self, token: str, claimed_uid: str) -> str:
        """Resolve an OIDC identity claim to the Nextcloud account UID.

        ``user_oidc`` maps an external IdP's ``sub`` to its own account UID;
        the two are equal only under the non-default
        ``--mapping-uid=sub --unique-uid=0``. Trusting the raw claim keys
        every downstream store (Qdrant filters, app-password lookups) on an
        identity Nextcloud itself does not use, and the failure is silent: a
        lookup under the wrong key returns zero results, not an error (#1326).

        Mirrors ``_validate_nextcloud_credentials``'s OCS v2 canonical-UID
        lookup in ``api/passwords.py``, but authenticates with the bearer
        token itself rather than a login/password pair: this module's own
        docstring establishes that token reuse against Nextcloud is safe
        (RFC 8707), and ``context_helper.py`` already reuses this exact
        token the same way once verification succeeds.

        Falls back to ``claimed_uid`` on any lookup failure, so an OCS
        outage degrades to the behavior before this method existed rather
        than rejecting an otherwise-valid token.

        Args:
            token: The bearer token to present to Nextcloud.
            claimed_uid: The unverified ``sub``/``preferred_username`` claim.

        Returns:
            The canonical Nextcloud UID, or ``claimed_uid`` if it could not
            be resolved.
        """
        nextcloud_host = getattr(self.settings, "nextcloud_host", None)
        if not nextcloud_host:
            return claimed_uid

        try:
            async with nextcloud_httpx_client(timeout=10.0) as client:
                response = await client.get(
                    f"{nextcloud_host.rstrip('/')}/ocs/v2.php/cloud/user",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "OCS-APIRequest": "true",
                    },
                    params={"format": "json"},
                )
        except Exception as e:
            # A dedicated, short-lived client (never self.http_client, which
            # is shared across every user's verification): Nextcloud sets a
            # session cookie on every OCS response, including failures, and a
            # long-lived client would replay user A's session on user B's
            # lookup. Broad except (not just httpx.RequestError): the
            # fallback to claimed_uid is safe by construction, so an
            # unexpected error here (a malformed NEXTCLOUD_HOST, a client
            # already closed, ...) should degrade the same way a network
            # error does rather than reject a token JWT/introspection
            # already validated.
            logger.warning(
                "Canonical-UID lookup failed for %s, using claimed value: %s",
                claimed_uid,
                e,
            )
            return claimed_uid

        if response.status_code != 200:
            logger.warning(
                "Canonical-UID lookup returned HTTP %s for %s, using claimed value",
                response.status_code,
                claimed_uid,
            )
            return claimed_uid

        try:
            payload = response.json()
        except ValueError:
            logger.warning(
                "Canonical-UID lookup returned a non-JSON body for %s, using "
                "claimed value",
                claimed_uid,
            )
            return claimed_uid

        # Parsed defensively, same shape as _validate_nextcloud_credentials:
        # a malformed OCS body must degrade, never raise.
        ocs = payload.get("ocs") if isinstance(payload, dict) else None
        ocs_data = ocs.get("data") if isinstance(ocs, dict) else None
        canonical_uid = ocs_data.get("id") if isinstance(ocs_data, dict) else None
        if not canonical_uid:
            logger.warning(
                "Canonical-UID lookup returned no id for %s, using claimed value",
                claimed_uid,
            )
            return claimed_uid

        if canonical_uid != claimed_uid:
            logger.info(
                "Resolved OIDC claim %s to canonical Nextcloud UID %s",
                claimed_uid,
                canonical_uid,
            )
        return canonical_uid

    def _create_access_token(
        self,
        token: str,
        payload: dict[str, Any],
        *,
        resolved_username: str | None = None,
    ) -> AccessToken | None:
        """
        Create AccessToken object from validated token payload.

        Args:
            token: The bearer token
            payload: Validated token payload
            resolved_username: Canonical Nextcloud UID from
                :meth:`_resolve_canonical_uid`, when the caller already ran
                that (async) lookup. Falls back to the raw claim when omitted.

        Returns:
            AccessToken object or None if required fields missing
        """
        # Use default cache key (hash of token)
        cache_key = hashlib.sha256(token.encode()).hexdigest()
        return self._create_access_token_with_cache_key(
            token, payload, cache_key, resolved_username=resolved_username
        )

    def _create_access_token_with_cache_key(
        self,
        token: str,
        payload: dict[str, Any],
        cache_key: str,
        *,
        via_userinfo: bool = False,
        resolved_username: str | None = None,
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
            resolved_username: Canonical Nextcloud UID from
                :meth:`_resolve_canonical_uid`, when the caller already ran
                that (async) lookup. Falls back to the raw claim when omitted,
                so this stays sync and callers that never resolve (tests,
                cache reconstruction) are unaffected.

        Returns:
            AccessToken object or None if required fields missing
        """
        # Extract username (sub claim, with fallback to preferred_username)
        username = (
            resolved_username or payload.get("sub") or payload.get("preferred_username")
        )
        if not username:
            # Deliberately silent: every caller routes a None result through
            # _reject(), whose WARNING carries the client_id and reason this
            # line lacked. Logging here too would give one rejection two lines
            # — the shape already removed from the bad_audience path.
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
                if k not in ("sub", "scope", "_auth_via_userinfo", "preferred_username")
            },
        }
        if via_userinfo:
            userinfo["_auth_via_userinfo"] = True
        self._token_cache[cache_key] = (userinfo, exp)

        return AccessToken(
            token=token,
            client_id=self._client_id_from_claims(payload),
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
            client_id=self._client_id_from_claims(userinfo),
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
