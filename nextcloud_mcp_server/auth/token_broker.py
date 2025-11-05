"""
Token Broker Service for ADR-004 Progressive Consent Architecture.

This service manages the lifecycle of Nextcloud access tokens, implementing
the dual OAuth flow pattern where:
1. MCP clients authenticate to MCP server with aud:"mcp-server" tokens
2. MCP server uses stored refresh tokens to obtain aud:"nextcloud" tokens

The Token Broker provides:
- Automatic token refresh when expired
- Short-lived token caching (5-minute TTL)
- Master refresh token rotation
- Audience-specific token validation
- Session vs background token separation (RFC 8693)
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import httpx
import jwt
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage
from nextcloud_mcp_server.auth.token_exchange import exchange_token_for_delegation

logger = logging.getLogger(__name__)


class TokenCache:
    """In-memory cache for short-lived Nextcloud access tokens."""

    def __init__(self, ttl_seconds: int = 300, early_refresh_seconds: int = 30):
        """
        Initialize the token cache.

        Args:
            ttl_seconds: Default TTL for cached tokens (5 minutes default)
            early_refresh_seconds: How many seconds before expiry to trigger early refresh (30s default)
        """
        self._cache: Dict[str, Tuple[str, datetime]] = {}
        self._ttl = timedelta(seconds=ttl_seconds)
        self._early_refresh = timedelta(seconds=early_refresh_seconds)
        self._lock = asyncio.Lock()

    async def get(self, user_id: str) -> Optional[str]:
        """Get cached token if valid."""
        async with self._lock:
            if user_id not in self._cache:
                return None

            token, expiry = self._cache[user_id]
            now = datetime.now(timezone.utc)

            # Check if token has expired
            if now >= expiry:
                del self._cache[user_id]
                logger.debug(f"Cached token expired for user {user_id}")
                return None

            # Check if token will expire soon (refresh early)
            if now >= expiry - self._early_refresh:
                logger.debug(f"Cached token expiring soon for user {user_id}")
                return None

            logger.debug(f"Using cached token for user {user_id}")
            return token

    async def set(self, user_id: str, token: str, expires_in: int | None = None):
        """Store token in cache."""
        async with self._lock:
            # Use provided expiry or default TTL
            if expires_in:
                expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            else:
                expiry = datetime.now(timezone.utc) + self._ttl

            self._cache[user_id] = (token, expiry)
            logger.debug(f"Cached token for user {user_id} until {expiry}")

    async def invalidate(self, user_id: str):
        """Remove token from cache."""
        async with self._lock:
            if user_id in self._cache:
                del self._cache[user_id]
                logger.debug(f"Invalidated cached token for user {user_id}")


class TokenBrokerService:
    """
    Manages token lifecycle for the Progressive Consent architecture.

    This service handles:
    - Getting or refreshing Nextcloud access tokens
    - Managing a short-lived token cache
    - Refreshing master refresh tokens periodically
    - Validating token audiences
    """

    def __init__(
        self,
        storage: RefreshTokenStorage,
        oidc_discovery_url: str,
        nextcloud_host: str,
        encryption_key: str,
        cache_ttl: int = 300,
        cache_early_refresh: int = 30,
    ):
        """
        Initialize the Token Broker Service.

        Args:
            storage: Database storage for refresh tokens
            oidc_discovery_url: OIDC provider discovery URL
            nextcloud_host: Nextcloud server URL
            encryption_key: Fernet key for token encryption
            cache_ttl: Cache TTL in seconds (default: 5 minutes)
            cache_early_refresh: Early refresh threshold in seconds (default: 30 seconds)
        """
        self.storage = storage
        self.oidc_discovery_url = oidc_discovery_url
        self.nextcloud_host = nextcloud_host
        self.fernet = Fernet(
            encryption_key.encode()
            if isinstance(encryption_key, str)
            else encryption_key
        )
        self.cache = TokenCache(cache_ttl, cache_early_refresh)
        self._oidc_config = None
        self._http_client = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0), follow_redirects=True
            )
        return self._http_client

    async def _get_oidc_config(self) -> dict:
        """Get OIDC configuration from discovery endpoint."""
        if self._oidc_config is None:
            client = await self._get_http_client()
            response = await client.get(self.oidc_discovery_url)
            response.raise_for_status()
            self._oidc_config = response.json()
        return self._oidc_config

    async def get_nextcloud_token(self, user_id: str) -> Optional[str]:
        """
        Get a valid Nextcloud access token for the user.

        DEPRECATED: This method uses the old pattern of stored refresh tokens
        for all operations. Use get_session_token() or get_background_token()
        instead for proper session/background separation.

        This method:
        1. Checks the cache for a valid token
        2. If not cached, checks for stored refresh token
        3. If refresh token exists, obtains new access token
        4. Caches the new token for future requests

        Args:
            user_id: The user identifier

        Returns:
            Valid Nextcloud access token or None if not provisioned
        """
        # Check cache first
        cached_token = await self.cache.get(user_id)
        if cached_token:
            return cached_token

        # Get stored refresh token
        refresh_data = await self.storage.get_refresh_token(user_id)
        if not refresh_data:
            logger.info(f"No refresh token found for user {user_id}")
            return None

        try:
            # Decrypt refresh token
            encrypted_token = refresh_data["refresh_token"]
            refresh_token = self.fernet.decrypt(encrypted_token.encode()).decode()

            # Exchange refresh token for new access token
            access_token, expires_in = await self._refresh_access_token(refresh_token)

            # Cache the new token
            await self.cache.set(user_id, access_token, expires_in)

            return access_token

        except Exception as e:
            logger.error(f"Failed to get Nextcloud token for user {user_id}: {e}")
            # Invalidate cache on error
            await self.cache.invalidate(user_id)
            return None

    async def get_session_token(
        self,
        flow1_token: str,
        required_scopes: list[str],
        requested_audience: str = "nextcloud",
    ) -> Optional[str]:
        """
        Get ephemeral token for MCP session operations (on-demand).

        This implements the correct Progressive Consent pattern where:
        1. Client provides Flow 1 token (aud: "mcp-server")
        2. Server exchanges it for ephemeral Nextcloud token
        3. Token is NOT stored, only used for current operation

        Key properties:
        - On-demand generation during tool execution
        - Ephemeral (not stored, discarded after use)
        - Limited scopes (only what tool needs)
        - Short-lived (5 minutes)

        Args:
            flow1_token: The MCP session token (aud: "mcp-server")
            required_scopes: Minimal scopes needed for this operation
            requested_audience: Target audience (usually "nextcloud")

        Returns:
            Ephemeral Nextcloud access token or None if exchange fails
        """
        try:
            # Perform RFC 8693 token exchange
            delegated_token, expires_in = await exchange_token_for_delegation(
                flow1_token=flow1_token,
                requested_scopes=required_scopes,
                requested_audience=requested_audience,
            )

            # NOTE: We intentionally do NOT cache session tokens
            # They are ephemeral and should be discarded after use
            logger.info(
                f"Generated ephemeral session token with scopes: {required_scopes}, "
                f"expires in {expires_in}s"
            )

            return delegated_token

        except Exception as e:
            logger.error(f"Failed to get session token: {e}")
            return None

    async def get_background_token(
        self, user_id: str, required_scopes: list[str]
    ) -> Optional[str]:
        """
        Get token for background job operations (uses stored refresh token).

        This is for background/offline operations that run without user interaction.
        Uses the stored refresh token from Flow 2 provisioning.

        Key properties:
        - Uses stored refresh token from Flow 2
        - Different scopes than session tokens
        - Longer-lived for background operations
        - Can be cached for efficiency

        Args:
            user_id: The user identifier
            required_scopes: Scopes needed for background operation

        Returns:
            Nextcloud access token for background operations or None if not provisioned
        """
        # Check cache first (background tokens can be cached)
        cache_key = f"{user_id}:background:{','.join(sorted(required_scopes))}"
        cached_token = await self.cache.get(cache_key)
        if cached_token:
            return cached_token

        # Get stored refresh token
        refresh_data = await self.storage.get_refresh_token(user_id)
        if not refresh_data:
            logger.info(f"No refresh token found for user {user_id}")
            return None

        try:
            # Decrypt refresh token
            encrypted_token = refresh_data["refresh_token"]
            refresh_token = self.fernet.decrypt(encrypted_token.encode()).decode()

            # Get token with specific scopes for background operation
            access_token, expires_in = await self._refresh_access_token_with_scopes(
                refresh_token, required_scopes
            )

            # Cache the background token
            await self.cache.set(cache_key, access_token, expires_in)

            logger.info(
                f"Generated background token for user {user_id} with scopes: {required_scopes}"
            )

            return access_token

        except Exception as e:
            logger.error(f"Failed to get background token for user {user_id}: {e}")
            await self.cache.invalidate(cache_key)
            return None

    async def _refresh_access_token(self, refresh_token: str) -> Tuple[str, int]:
        """
        Exchange refresh token for new access token.

        DEPRECATED: Use _refresh_access_token_with_scopes() for scope-specific requests.

        Args:
            refresh_token: The refresh token

        Returns:
            Tuple of (access_token, expires_in_seconds)
        """
        config = await self._get_oidc_config()
        token_endpoint = config["token_endpoint"]

        client = await self._get_http_client()

        # Request new access token using refresh token
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "openid profile email notes:read notes:write calendar:read calendar:write",
        }

        response = await client.post(
            token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            logger.error(
                f"Token refresh failed: {response.status_code} - {response.text}"
            )
            raise Exception(f"Token refresh failed: {response.status_code}")

        token_data = response.json()
        access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 3600)  # Default 1 hour

        # Validate audience
        await self._validate_token_audience(access_token, "nextcloud")

        logger.info(f"Refreshed access token (expires in {expires_in}s)")
        return access_token, expires_in

    async def _refresh_access_token_with_scopes(
        self, refresh_token: str, required_scopes: list[str]
    ) -> Tuple[str, int]:
        """
        Exchange refresh token for new access token with specific scopes.

        This method implements scope downscoping for least privilege.

        Args:
            refresh_token: The refresh token
            required_scopes: Minimal scopes needed for this operation

        Returns:
            Tuple of (access_token, expires_in_seconds)
        """
        config = await self._get_oidc_config()
        token_endpoint = config["token_endpoint"]

        client = await self._get_http_client()

        # Always include basic OpenID scopes
        scopes = list(set(["openid", "profile", "email"] + required_scopes))

        # Request new access token with specific scopes
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(scopes),
        }

        response = await client.post(
            token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            logger.error(
                f"Token refresh with scopes failed: {response.status_code} - {response.text}"
            )
            raise Exception(f"Token refresh failed: {response.status_code}")

        token_data = response.json()
        access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 3600)  # Default 1 hour

        # Validate audience
        await self._validate_token_audience(access_token, "nextcloud")

        logger.info(
            f"Refreshed access token with scopes {scopes} (expires in {expires_in}s)"
        )
        return access_token, expires_in

    async def _validate_token_audience(self, token: str, expected_audience: str):
        """
        Validate that token has correct audience claim.

        Args:
            token: JWT token to validate
            expected_audience: Expected audience value

        Raises:
            ValueError: If audience doesn't match
        """
        try:
            # Decode without verification to check claims
            # In production, should verify signature
            claims = jwt.decode(token, options={"verify_signature": False})

            audience = claims.get("aud", [])
            if isinstance(audience, str):
                audience = [audience]

            if expected_audience not in audience:
                raise ValueError(
                    f"Token audience {audience} doesn't include {expected_audience}"
                )

        except jwt.DecodeError as e:
            # Token might be opaque, skip validation
            logger.debug(f"Cannot decode token for audience validation: {e}")

    async def refresh_master_token(self, user_id: str) -> bool:
        """
        Refresh the master refresh token (periodic rotation).

        This should be called periodically (e.g., daily) to rotate
        refresh tokens for security.

        Args:
            user_id: The user identifier

        Returns:
            True if refresh successful, False otherwise
        """
        refresh_data = await self.storage.get_refresh_token(user_id)
        if not refresh_data:
            logger.warning(f"No refresh token to rotate for user {user_id}")
            return False

        try:
            # Decrypt current refresh token
            encrypted_token = refresh_data["refresh_token"]
            current_refresh_token = self.fernet.decrypt(
                encrypted_token.encode()
            ).decode()

            # Get OIDC configuration
            config = await self._get_oidc_config()
            token_endpoint = config["token_endpoint"]

            client = await self._get_http_client()

            # Request new refresh token
            data = {
                "grant_type": "refresh_token",
                "refresh_token": current_refresh_token,
                "scope": "openid profile email offline_access notes:read notes:write calendar:read calendar:write",
            }

            response = await client.post(
                token_endpoint,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if response.status_code != 200:
                logger.error(f"Master token refresh failed: {response.status_code}")
                return False

            token_data = response.json()
            new_refresh_token = token_data.get("refresh_token")

            if new_refresh_token and new_refresh_token != current_refresh_token:
                # Encrypt and store new refresh token
                encrypted_new = self.fernet.encrypt(new_refresh_token.encode()).decode()
                await self.storage.store_refresh_token(
                    user_id=user_id,
                    refresh_token=encrypted_new,
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(days=90),  # 90-day expiry
                )
                logger.info(f"Rotated master refresh token for user {user_id}")

                # Invalidate cached access token
                await self.cache.invalidate(user_id)
                return True

            return True

        except Exception as e:
            logger.error(f"Failed to refresh master token for user {user_id}: {e}")
            return False

    async def has_nextcloud_provisioning(self, user_id: str) -> bool:
        """
        Check if user has provisioned Nextcloud access (Flow 2).

        Args:
            user_id: The user identifier

        Returns:
            True if user has stored refresh token, False otherwise
        """
        refresh_data = await self.storage.get_refresh_token(user_id)
        return refresh_data is not None

    async def revoke_nextcloud_access(self, user_id: str) -> bool:
        """
        Revoke stored Nextcloud access for a user.

        This removes stored refresh tokens and clears cache.

        Args:
            user_id: The user identifier

        Returns:
            True if revocation successful
        """
        try:
            # Get refresh token for revocation at IdP
            refresh_data = await self.storage.get_refresh_token(user_id)
            if refresh_data:
                try:
                    # Attempt to revoke at IdP
                    encrypted_token = refresh_data["refresh_token"]
                    refresh_token = self.fernet.decrypt(
                        encrypted_token.encode()
                    ).decode()
                    await self._revoke_token_at_idp(refresh_token)
                except Exception as e:
                    logger.warning(f"Failed to revoke at IdP: {e}")

            # Remove from storage
            await self.storage.delete_refresh_token(user_id)

            # Clear cache
            await self.cache.invalidate(user_id)

            logger.info(f"Revoked Nextcloud access for user {user_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to revoke access for user {user_id}: {e}")
            return False

    async def _revoke_token_at_idp(self, token: str):
        """Revoke token at the IdP if revocation endpoint exists."""
        config = await self._get_oidc_config()
        revocation_endpoint = config.get("revocation_endpoint")

        if not revocation_endpoint:
            logger.debug("No revocation endpoint available")
            return

        client = await self._get_http_client()

        data = {"token": token, "token_type_hint": "refresh_token"}

        response = await client.post(
            revocation_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code == 200:
            logger.info("Token revoked at IdP")
        else:
            logger.warning(f"Token revocation returned {response.status_code}")

    async def close(self):
        """Clean up resources."""
        if self._http_client:
            await self._http_client.aclose()
