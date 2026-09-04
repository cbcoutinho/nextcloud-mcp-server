"""Token utility functions for extracting user identity from MCP access tokens.

Extracted from server/oauth_tools.py to break circular import dependencies
between server/ and auth/ layers.
"""

import logging
import secrets
import time
from typing import Any

import anyio
import jwt
from jwt import PyJWKSet
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


# OIDC discovery + JWKS caches keyed by URL → (expires_at, data). Single
# source of truth for the codebase: oauth_routes / browser_oauth_routes both
# go through ``get_oidc_discovery`` which reads/writes _discovery_cache, so
# the first discovery fetch primes the cache for all later callers (PR #758
# round-2 nit 3). 5-minute TTL.
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_OIDC_CACHE_TTL = 300

# Per-URL fetch locks coalesce concurrent cache misses into a single HTTP
# request, preventing thundering-herd against the IdP at cache expiry
# (PR #758 round-3 review). Mirrors the lock-dict + meta-lock idiom from
# token_broker.py.
_fetch_locks: dict[str, anyio.Lock] = {}
_fetch_locks_lock = anyio.Lock()


async def _get_fetch_lock(url: str) -> anyio.Lock:
    """Return the per-URL lock used to serialise cache-miss fetches."""
    async with _fetch_locks_lock:
        lock = _fetch_locks.get(url)
        if lock is None:
            lock = anyio.Lock()
            _fetch_locks[url] = lock
        return lock


class IdTokenVerificationError(Exception):
    """Raised when an OIDC ID token fails signature or claim verification."""


async def _get_cached(
    cache: dict[str, tuple[float, dict[str, Any]]],
    url: str,
    *,
    follow_redirects: bool = False,
) -> dict[str, Any]:
    """Return cached JSON response for *url* or fetch + cache on miss/expiry.

    ``follow_redirects`` is forwarded to ``nextcloud_httpx_client``: discovery
    fetches against Nextcloud without pretty URLs need it (the configured
    ``/.well-known/openid-configuration`` path issues a 301), but JWKS
    fetches deliberately stay strict — the URL came from the discovery
    document we already trust, so a redirect there would be suspicious.

    Concurrent callers seeing the same cache miss are coalesced via a
    per-URL ``anyio.Lock``: only one fetch runs, the rest wait and read the
    populated cache.
    """
    entry = cache.get(url)
    if entry is not None and time.time() < entry[0]:
        return entry[1]
    lock = await _get_fetch_lock(url)
    try:
        async with lock:
            # Re-check inside the lock — a concurrent waiter may have already
            # populated the cache before we acquired it.
            entry = cache.get(url)
            if entry is not None and time.time() < entry[0]:
                return entry[1]
            async with nextcloud_httpx_client(
                follow_redirects=follow_redirects
            ) as http_client:
                response = await http_client.get(url)
                response.raise_for_status()
                data = response.json()
            cache[url] = (time.time() + _OIDC_CACHE_TTL, data)
            return data
    finally:
        # Drop the dict entry so a misconfigured deployment hitting
        # arbitrary URLs can't grow ``_fetch_locks`` without bound (PR #758
        # round-4 review nit 4). Already-queued waiters share our local
        # ``lock`` reference and remain coalesced; new arrivals lazily
        # recreate a lock — by which time the cache is populated, so they
        # short-circuit before reaching the lock anyway.
        async with _fetch_locks_lock:
            if _fetch_locks.get(url) is lock:
                del _fetch_locks[url]


async def get_oidc_discovery(discovery_url: str) -> dict[str, Any]:
    """Return the cached OIDC discovery document for *discovery_url*.

    Shares the 5-minute discovery cache used by `verify_id_token`, so a
    callback that does discovery → token-exchange → ID-token verification
    reuses one HTTP round-trip instead of three. The fetch follows
    redirects because Nextcloud without pretty URLs returns 301 from
    ``/.well-known/openid-configuration`` to ``/index.php/.well-known/...``.
    Single source of truth for OIDC discovery in the codebase
    (PR #758 round-2 nit 3).
    """
    return await _get_cached(_discovery_cache, discovery_url, follow_redirects=True)


async def verify_id_token(
    id_token: str | None,
    *,
    discovery_url: str,
    expected_audience: str,
    expected_nonce: str | None = None,
) -> dict[str, Any]:
    """Verify an OIDC ID token's signature and standard claims.

    Implements the verification steps required by OIDC core spec section
    3.1.3.7 (ID Token Validation) for the authorization-code flow:
      - Signature against JWKS (RS256)
      - Issuer matches the OP that issued the token
      - Audience contains the expected client_id
      - Token is not expired (`exp`)
      - `iat` is well-formed (PyJWT default)
      - `nonce` matches when one was included in the auth request

    Replaces the prior `jwt.decode(id_token, options={"verify_signature": False})`
    pattern (issue #626 finding 1) on the OAuth callback paths.

    Args:
        id_token: Raw ID token (JWT) string.
        discovery_url: OIDC `.well-known/openid-configuration` URL of the IdP.
        expected_audience: The MCP-server-side OAuth client_id used for this
            authorization request.
        expected_nonce: When the auth request included a nonce, the same value
            so it can be checked here. None disables the nonce check (callers
            that didn't bind a nonce in the auth request).

    Returns:
        Decoded, verified ID-token claims.

    Raises:
        IdTokenVerificationError: On any verification failure.
    """
    if not id_token:
        raise IdTokenVerificationError("ID token missing from token response")

    try:
        discovery = await get_oidc_discovery(discovery_url)

        issuer = discovery.get("issuer")
        jwks_uri = discovery.get("jwks_uri")
        if not issuer or not jwks_uri:
            raise IdTokenVerificationError(
                "OIDC discovery response missing issuer or jwks_uri"
            )

        jwks_data = await _get_cached(_jwks_cache, jwks_uri)
    except IdTokenVerificationError:
        raise
    except Exception as e:
        raise IdTokenVerificationError(
            f"Failed to fetch OIDC discovery / JWKS: {e}"
        ) from e

    try:
        jwks = PyJWKSet.from_dict(jwks_data)
        unverified_header = jwt.get_unverified_header(id_token)
        kid = unverified_header.get("kid")
        if not kid:
            raise IdTokenVerificationError("ID token header missing 'kid'")
        try:
            signing_key = jwks[kid]
        except KeyError:
            # Cache miss may indicate IdP key rotation. Refresh JWKS once
            # before giving up, per OIDC core §10.1.1: when an unrecognised
            # `kid` arrives the relying party should refetch the JWKS rather
            # than waiting for cache TTL to elapse.
            _jwks_cache.pop(jwks_uri, None)
            try:
                jwks_data = await _get_cached(_jwks_cache, jwks_uri)
                jwks = PyJWKSet.from_dict(jwks_data)
                signing_key = jwks[kid]
            except KeyError as e:
                raise IdTokenVerificationError(
                    f"No JWKS key matches ID token kid {kid!r}"
                ) from e
            except Exception as e:
                raise IdTokenVerificationError(
                    f"Failed to refresh JWKS after kid miss: {e}"
                ) from e

        # PyJWT verifies the JWT with the algorithm declared in its header,
        # cross-checked against this allowlist (so an attacker can't downgrade
        # to ``none`` or HMAC). The allowlist covers the OIDC algorithms
        # most cloud IdPs ship by default:
        #   - RS256: Nextcloud user_oidc, Keycloak default, Auth0, Google.
        #   - PS256: Azure AD on newer keys.
        #   - ES256: some Keycloak realms, AWS Cognito user pools.
        # Symmetric (HSxxx) and ``none`` are intentionally absent.
        payload: dict[str, Any] = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "PS256", "ES256"],
            audience=expected_audience,
            issuer=issuer,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "require": ["sub", "iss", "aud", "exp", "iat"],
            },
        )
    except IdTokenVerificationError:
        raise
    except jwt.PyJWTError as e:
        raise IdTokenVerificationError(f"ID token verification failed: {e}") from e
    except Exception as e:
        raise IdTokenVerificationError(
            f"Unexpected error verifying ID token: {e}"
        ) from e

    # Constant-time comparison mirrors the PKCE verifier check
    # (oauth_routes.py:1029) — short-circuit `!=` is avoided in
    # security-sensitive equality even when the secret is server-generated
    # (round-6 review).
    if expected_nonce is not None and not secrets.compare_digest(
        payload.get("nonce", "") or "", expected_nonce
    ):
        raise IdTokenVerificationError("ID token nonce does not match request nonce")

    return payload


async def extract_user_id_from_token(_ctx: Context) -> str:
    """Extract user_id from the verified MCP access token.

    Reads the `sub` claim from `AccessToken.resource`, which is populated by
    `UnifiedTokenVerifier` after JWT signature verification (or token
    introspection for opaque tokens). We never re-decode the raw token here:
    the verifier has already validated the signature and extracted the
    identity claim.

    Args:
        _ctx: MCP context with access token. Intentionally unused — kept on
            the public signature so call sites can pass the MCPServer Context
            they already hold without rewriting; identity is read from the
            verifier-populated AccessToken via get_access_token().

    Returns:
        user_id from the verified token, or ``"default_user"`` when no
        access token is present at all (BasicAuth mode — there is no
        OAuth identity to extract, so the sentinel is returned and the
        caller's BasicAuth branch handles it).

    Raises:
        MCPError: An access token was present but had no ``sub`` claim
            (``access_token.resource`` empty). Failing closed prevents a
            malformed IdP token from silently bucketing every request
            under the ``"default_user"`` key in SQLite, which would risk
            cross-tenant data exposure (PR #758 follow-up review).
    """
    access_token: AccessToken | None = get_access_token()

    if not access_token:
        logger.warning("No access token found via get_access_token()")
        return "default_user"

    user_id = access_token.resource
    if not user_id:
        logger.error(
            "Access token has no resource (sub) claim — verifier should have rejected it"
        )
        raise MCPError(
            # JSON-RPC 2.0 reserves -32000..-32099 for application errors.
            code=-32001,
            message="Cannot determine user identity from access token",
        )

    return user_id
