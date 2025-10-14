"""Token verification using Nextcloud OIDC userinfo endpoint."""

import logging
import time
from typing import Any

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)


class NextcloudTokenVerifier(TokenVerifier):
    """
    Validates access tokens using Nextcloud OIDC userinfo endpoint.

    This verifier:
    1. Calls the userinfo endpoint with the bearer token
    2. Caches successful responses to avoid repeated API calls
    3. Extracts username from the 'sub' or 'preferred_username' claim
    4. Optionally supports JWT validation for performance (future enhancement)

    The userinfo endpoint validates the token and returns user claims if valid,
    or returns HTTP 400/401 if the token is invalid or expired.
    """

    def __init__(
        self,
        nextcloud_host: str,
        userinfo_uri: str,
        cache_ttl: int = 3600,
    ):
        """
        Initialize the token verifier.

        Args:
            nextcloud_host: Base URL of the Nextcloud instance (e.g., https://cloud.example.com)
            userinfo_uri: Full URL to the userinfo endpoint
            cache_ttl: Time-to-live for cached tokens in seconds (default: 3600)
        """
        self.nextcloud_host = nextcloud_host.rstrip("/")
        self.userinfo_uri = userinfo_uri
        self.cache_ttl = cache_ttl

        # Cache: token -> (userinfo, expiry_timestamp)
        self._token_cache: dict[str, tuple[dict[str, Any], float]] = {}

        # HTTP client for userinfo requests
        self._client = httpx.AsyncClient(timeout=10.0)

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Verify a bearer token by calling the userinfo endpoint.

        This method:
        1. Checks the cache first for recent validations
        2. Calls the userinfo endpoint if not cached
        3. Returns AccessToken with username stored in metadata

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

        # Validate via userinfo endpoint
        try:
            return await self._verify_via_userinfo(token)
        except Exception as e:
            logger.warning(f"Token verification failed: {e}")
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
