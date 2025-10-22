"""Token verification using Nextcloud OIDC userinfo endpoint."""

import logging
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)


class NextcloudTokenVerifier(TokenVerifier):
    """
    Validates access tokens using JWT verification with JWKS or userinfo endpoint fallback.

    This verifier supports both JWT and opaque tokens:
    1. For JWT tokens: Verifies signature with JWKS and extracts scopes from payload
    2. For opaque tokens: Falls back to userinfo endpoint validation
    3. Caches successful responses to avoid repeated API calls/verifications

    JWT validation provides:
    - Faster validation (no HTTP call needed)
    - Direct scope extraction from token payload
    - Signature verification using JWKS

    Userinfo fallback provides:
    - Support for opaque tokens
    - Backward compatibility
    - Additional validation layer
    """

    def __init__(
        self,
        nextcloud_host: str,
        userinfo_uri: str,
        jwks_uri: str | None = None,
        issuer: str | None = None,
        cache_ttl: int = 3600,
    ):
        """
        Initialize the token verifier.

        Args:
            nextcloud_host: Base URL of the Nextcloud instance (e.g., https://cloud.example.com)
            userinfo_uri: Full URL to the userinfo endpoint
            jwks_uri: Full URL to the JWKS endpoint (for JWT verification)
            issuer: Expected issuer claim value (for JWT verification)
            cache_ttl: Time-to-live for cached tokens in seconds (default: 3600)
        """
        self.nextcloud_host = nextcloud_host.rstrip("/")
        self.userinfo_uri = userinfo_uri
        self.jwks_uri = jwks_uri
        self.issuer = issuer
        self.cache_ttl = cache_ttl

        # Cache: token -> (userinfo, expiry_timestamp)
        self._token_cache: dict[str, tuple[dict[str, Any], float]] = {}

        # HTTP client for userinfo requests
        self._client = httpx.AsyncClient(timeout=10.0)

        # PyJWKClient for JWT verification (lazy initialization)
        self._jwks_client: PyJWKClient | None = None
        if jwks_uri:
            logger.info(f"JWT verification enabled with JWKS URI: {jwks_uri}")
            self._jwks_client = PyJWKClient(jwks_uri, cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Verify a bearer token using JWT verification or userinfo endpoint.

        This method:
        1. Checks the cache first for recent validations
        2. Attempts JWT verification if JWKS is configured and token looks like JWT
        3. Falls back to userinfo endpoint for opaque tokens or JWT verification failures
        4. Returns AccessToken with username and scopes

        Args:
            token: The bearer token to verify

        Returns:
            AccessToken if valid, None if invalid or expired
        """
        # Check cache first
        cached = self._get_cached_token(token)
        if cached:
            logger.debug("Token found in cache")
            return cached

        # Try JWT verification first if enabled and token looks like JWT
        if self._jwks_client and self._is_jwt_format(token):
            logger.debug("Attempting JWT verification...")
            jwt_result = self._verify_jwt(token)
            if jwt_result:
                logger.info("Token validated via JWT verification")
                return jwt_result

        # Fall back to userinfo endpoint validation
        logger.debug("Attempting userinfo endpoint validation...")
        try:
            return await self._verify_via_userinfo(token)
        except Exception as e:
            logger.warning(f"Token verification failed: {e}")
            return None

    def _is_jwt_format(self, token: str) -> bool:
        """
        Check if token looks like a JWT (has 3 parts separated by dots).

        Args:
            token: The token to check

        Returns:
            True if token appears to be JWT format
        """
        return "." in token and token.count(".") == 2

    def _verify_jwt(self, token: str) -> AccessToken | None:
        """
        Verify JWT token with signature validation using JWKS.

        Args:
            token: The JWT token to verify

        Returns:
            AccessToken if valid, None if invalid
        """
        try:
            # Get signing key from JWKS
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)

            # Verify and decode JWT
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True if self.issuer else False,
                    "verify_aud": False,  # Skip audience validation for Bearer tokens
                },
            )

            logger.debug(f"JWT verified successfully for user: {payload.get('sub')}")

            # Extract username (sub claim)
            username = payload.get("sub")
            if not username:
                logger.error("No 'sub' claim found in JWT payload")
                return None

            # Extract scopes from scope claim (space-separated string)
            scope_string = payload.get("scope", "")
            scopes = scope_string.split() if scope_string else []
            logger.debug(f"Extracted scopes from JWT: {scopes}")

            # Extract expiration
            exp = payload.get("exp")
            if not exp:
                logger.warning("No 'exp' claim in JWT, using default TTL")
                exp = int(time.time() + self.cache_ttl)

            # Cache the result
            userinfo = {
                "sub": username,
                "scope": scope_string,
                **{k: v for k, v in payload.items() if k not in ["sub", "scope"]},
            }
            self._token_cache[token] = (userinfo, exp)

            return AccessToken(
                token=token,
                client_id=payload.get("client_id", ""),
                scopes=scopes,
                expires_at=exp,
                resource=username,  # Store username in resource field (RFC 8707)
            )

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

    async def _verify_via_userinfo(self, token: str) -> AccessToken | None:
        """
        Validate token by calling the userinfo endpoint.

        Args:
            token: The bearer token to verify

        Returns:
            AccessToken if valid, None otherwise
        """
        try:
            response = await self._client.get(
                self.userinfo_uri, headers={"Authorization": f"Bearer {token}"}
            )

            if response.status_code == 200:
                userinfo = response.json()
                logger.debug(
                    f"Token validated successfully for user: {userinfo.get('sub')}"
                )

                # Cache the result
                expiry = time.time() + self.cache_ttl
                self._token_cache[token] = (userinfo, expiry)

                # Create AccessToken with username in resource field (workaround for MCP SDK)
                username = userinfo.get("sub") or userinfo.get("preferred_username")
                if not username:
                    logger.error("No username found in userinfo response")
                    return None

                return AccessToken(
                    token=token,
                    client_id="",  # Not available from userinfo
                    scopes=self._extract_scopes(userinfo),
                    expires_at=int(expiry),
                    resource=username,  # Store username in resource field (RFC 8707)
                )

            elif response.status_code in (400, 401, 403):
                logger.info(f"Token validation failed: HTTP {response.status_code}")
                return None
            else:
                logger.warning(
                    f"Unexpected response from userinfo: {response.status_code}"
                )
                return None

        except httpx.TimeoutException:
            logger.error("Timeout while validating token via userinfo endpoint")
            return None
        except httpx.RequestError as e:
            logger.error(f"Network error while validating token: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during token validation: {e}")
            return None

    def _get_cached_token(self, token: str) -> AccessToken | None:
        """
        Retrieve a token from cache if not expired.

        Args:
            token: The bearer token to look up

        Returns:
            AccessToken if cached and valid, None otherwise
        """
        if token not in self._token_cache:
            return None

        userinfo, expiry = self._token_cache[token]

        # Check if expired
        if time.time() >= expiry:
            logger.debug("Cached token expired, removing from cache")
            del self._token_cache[token]
            return None

        # Return cached AccessToken
        username = userinfo.get("sub") or userinfo.get("preferred_username")
        return AccessToken(
            token=token,
            client_id="",
            scopes=self._extract_scopes(userinfo),
            expires_at=int(expiry),
            resource=username,
        )

    def _extract_scopes(self, userinfo: dict[str, Any]) -> list[str]:
        """
        Extract scopes from userinfo response.

        Since the userinfo response doesn't include the original scopes,
        we infer them from the claims present in the response.

        Args:
            userinfo: The userinfo response dictionary

        Returns:
            List of inferred scopes
        """
        scopes = ["openid"]  # Always present

        if "email" in userinfo:
            scopes.append("email")

        if any(
            key in userinfo for key in ["name", "given_name", "family_name", "picture"]
        ):
            scopes.append("profile")

        if "roles" in userinfo:
            scopes.append("roles")

        if "groups" in userinfo:
            scopes.append("groups")

        return scopes

    def clear_cache(self):
        """Clear the token cache."""
        self._token_cache.clear()
        logger.debug("Token cache cleared")

    async def close(self):
        """Cleanup resources."""
        await self._client.aclose()
        logger.debug("Token verifier closed")
