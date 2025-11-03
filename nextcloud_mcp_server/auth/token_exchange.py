"""RFC 8693 Token Exchange implementation for ADR-004 Progressive Consent.

This module implements the token exchange pattern to convert Flow 1 MCP tokens
(aud: "mcp-server") into ephemeral delegated Nextcloud tokens (aud: "nextcloud")
for session operations.

Key Properties:
- On-demand generation during tool execution
- Ephemeral tokens (NOT stored, discarded after use)
- Limited scopes (only what tool needs)
- Short-lived (5 minutes default)
"""

import logging
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

import httpx
import jwt

from ..config import get_settings
from .refresh_token_storage import RefreshTokenStorage

logger = logging.getLogger(__name__)


class TokenExchangeService:
    """Implements RFC 8693 OAuth 2.0 Token Exchange."""

    # RFC 8693 Token Type Identifiers
    TOKEN_TYPE_ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
    TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"
    TOKEN_TYPE_ID_TOKEN = "urn:ietf:params:oauth:token-type:id_token"

    def __init__(
        self,
        oidc_discovery_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        nextcloud_host: Optional[str] = None,
    ):
        """Initialize token exchange service.

        Args:
            oidc_discovery_url: OIDC discovery endpoint URL
            client_id: OAuth client ID for token exchange
            client_secret: OAuth client secret
            nextcloud_host: Nextcloud instance URL
        """
        settings = get_settings()
        self.oidc_discovery_url = oidc_discovery_url or settings.oidc_discovery_url
        self.client_id = client_id or settings.oidc_client_id
        self.client_secret = client_secret or settings.oidc_client_secret
        self.nextcloud_host = nextcloud_host or settings.nextcloud_host

        self._token_endpoint: Optional[str] = None
        self._jwks_uri: Optional[str] = None
        self._discovery_cache: Optional[Dict[str, Any]] = None
        self._discovery_cache_time: float = 0
        self._discovery_cache_ttl: float = 3600  # 1 hour

        # Initialize storage for checking provisioning
        self.storage = RefreshTokenStorage()

        # Create HTTP client
        self.http_client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
        )

    async def __aenter__(self):
        """Async context manager entry."""
        await self.storage.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    async def close(self):
        """Close HTTP client and storage."""
        await self.http_client.aclose()
        # RefreshTokenStorage doesn't have a close method

    async def _discover_endpoints(self) -> Dict[str, Any]:
        """Discover OIDC endpoints from discovery URL.

        Returns:
            Discovery document containing endpoint URLs
        """
        # Check cache
        if (
            self._discovery_cache
            and (time.time() - self._discovery_cache_time) < self._discovery_cache_ttl
        ):
            return self._discovery_cache

        if not self.oidc_discovery_url:
            # Fallback to Nextcloud OIDC if no discovery URL
            self.oidc_discovery_url = urljoin(
                self.nextcloud_host, "/.well-known/openid-configuration"
            )

        try:
            response = await self.http_client.get(self.oidc_discovery_url)
            response.raise_for_status()

            self._discovery_cache = response.json()
            self._discovery_cache_time = time.time()

            # Cache frequently used endpoints
            self._token_endpoint = self._discovery_cache.get("token_endpoint")
            self._jwks_uri = self._discovery_cache.get("jwks_uri")

            return self._discovery_cache

        except Exception as e:
            logger.error(f"Failed to discover OIDC endpoints: {e}")
            raise

    async def exchange_token_for_delegation(
        self,
        flow1_token: str,
        requested_scopes: list[str],
        requested_audience: str = "nextcloud",
    ) -> Tuple[str, int]:
        """Exchange Flow 1 MCP token for delegated Nextcloud token.

        This implements RFC 8693 Token Exchange for on-behalf-of delegation.

        Args:
            flow1_token: The MCP session token (aud: "mcp-server")
            requested_scopes: Scopes needed for this operation
            requested_audience: Target audience (usually "nextcloud")

        Returns:
            Tuple of (delegated_token, expires_in)

        Raises:
            ValueError: If token validation fails
            RuntimeError: If provisioning not completed or exchange fails
        """
        # 1. Validate Flow 1 token audience
        await self._validate_flow1_token(flow1_token)

        # 2. Extract user ID from token
        user_id = self._extract_user_id(flow1_token)

        # 3. Check user has provisioned Nextcloud access (Flow 2)
        if not await self._check_provisioning(user_id):
            raise RuntimeError(
                "Nextcloud access not provisioned. "
                "User must complete Flow 2 provisioning first."
            )

        # 4. Get stored refresh token for user (from Flow 2)
        refresh_token = await self._get_user_refresh_token(user_id)
        if not refresh_token:
            raise RuntimeError(
                "No refresh token found. User must complete provisioning."
            )

        # 5. Perform token exchange with IdP
        delegated_token, expires_in = await self._perform_token_exchange(
            subject_token=flow1_token,
            refresh_token=refresh_token,
            requested_scopes=requested_scopes,
            requested_audience=requested_audience,
        )

        # 6. Log the exchange for audit trail
        logger.info(
            f"Token exchange completed for user {user_id}: "
            f"scopes={requested_scopes}, audience={requested_audience}, "
            f"expires_in={expires_in}s"
        )

        return delegated_token, expires_in

    async def _validate_flow1_token(self, token: str):
        """Validate that token has correct audience for MCP server.

        Args:
            token: JWT token to validate

        Raises:
            ValueError: If token is invalid or has wrong audience
        """
        try:
            # Decode without verification first to check audience
            # In production, should verify signature against JWKS
            payload = jwt.decode(token, options={"verify_signature": False})

            # Check audience
            audience = payload.get("aud", [])
            if isinstance(audience, str):
                audience = [audience]

            if "mcp-server" not in audience:
                raise ValueError(
                    f"Invalid token audience. Expected 'mcp-server', got {audience}"
                )

            # Check expiration
            exp = payload.get("exp", 0)
            if exp < time.time():
                raise ValueError("Token has expired")

        except jwt.DecodeError as e:
            raise ValueError(f"Invalid JWT token: {e}")

    def _extract_user_id(self, token: str) -> str:
        """Extract user ID from JWT token.

        Args:
            token: JWT token

        Returns:
            User ID from token
        """
        try:
            payload = jwt.decode(token, options={"verify_signature": False})

            # Try standard claims in order of preference
            user_id = (
                payload.get("sub")
                or payload.get("preferred_username")
                or payload.get("email")
                or payload.get("name")
            )

            if not user_id:
                raise ValueError("No user identifier in token")

            return user_id

        except jwt.DecodeError as e:
            raise ValueError(f"Failed to extract user ID: {e}")

    async def _check_provisioning(self, user_id: str) -> bool:
        """Check if user has completed Flow 2 provisioning.

        Args:
            user_id: User identifier

        Returns:
            True if provisioned, False otherwise
        """
        token_data = await self.storage.get_refresh_token(user_id)
        return token_data is not None

    async def _get_user_refresh_token(self, user_id: str) -> Optional[str]:
        """Get stored refresh token for user from Flow 2 provisioning.

        Args:
            user_id: User identifier

        Returns:
            Refresh token if found, None otherwise
        """
        token_data = await self.storage.get_refresh_token(user_id)
        if token_data:
            return token_data.get("refresh_token")
        return None

    async def _perform_token_exchange(
        self,
        subject_token: str,
        refresh_token: str,
        requested_scopes: list[str],
        requested_audience: str,
    ) -> Tuple[str, int]:
        """Perform RFC 8693 token exchange with IdP.

        Args:
            subject_token: The token being exchanged (Flow 1 token)
            refresh_token: User's stored refresh token for delegation
            requested_scopes: Minimal scopes for this operation
            requested_audience: Target audience

        Returns:
            Tuple of (access_token, expires_in)
        """
        # Discover token endpoint
        discovery = await self._discover_endpoints()
        token_endpoint = discovery.get("token_endpoint")

        if not token_endpoint:
            raise RuntimeError("No token endpoint found in discovery")

        # Build token exchange request per RFC 8693
        data = {
            # Token exchange grant type
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            # The token we're exchanging (Flow 1 MCP token)
            "subject_token": subject_token,
            "subject_token_type": self.TOKEN_TYPE_ACCESS_TOKEN,
            # Use refresh token as actor token (proves we have delegation rights)
            "actor_token": refresh_token,
            "actor_token_type": self.TOKEN_TYPE_ACCESS_TOKEN,
            # Requested token properties
            "requested_token_type": self.TOKEN_TYPE_ACCESS_TOKEN,
            "audience": requested_audience,
            "scope": " ".join(requested_scopes),
        }

        # Add client credentials if configured
        if self.client_id and self.client_secret:
            data["client_id"] = self.client_id
            data["client_secret"] = self.client_secret

        try:
            # Attempt RFC 8693 token exchange
            response = await self.http_client.post(
                token_endpoint,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if response.status_code == 400:
                # Token exchange might not be supported, fall back to refresh grant
                logger.info(
                    "Token exchange not supported, falling back to refresh grant"
                )
                return await self._fallback_refresh_grant(
                    refresh_token=refresh_token,
                    requested_scopes=requested_scopes,
                    token_endpoint=token_endpoint,
                )

            response.raise_for_status()
            result = response.json()

            access_token = result.get("access_token")
            expires_in = result.get("expires_in", 300)  # Default 5 minutes

            if not access_token:
                raise RuntimeError("No access token in exchange response")

            return access_token, expires_in

        except httpx.HTTPStatusError as e:
            logger.error(f"Token exchange failed: {e.response.text}")
            raise RuntimeError(f"Token exchange failed: {e}")
        except Exception as e:
            logger.error(f"Token exchange error: {e}")
            raise

    async def _fallback_refresh_grant(
        self, refresh_token: str, requested_scopes: list[str], token_endpoint: str
    ) -> Tuple[str, int]:
        """Fallback to standard refresh token grant if token exchange not supported.

        This is less secure than token exchange but provides compatibility.

        Args:
            refresh_token: User's stored refresh token
            requested_scopes: Minimal scopes for this operation
            token_endpoint: Token endpoint URL

        Returns:
            Tuple of (access_token, expires_in)
        """
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(requested_scopes),  # Request minimal scopes
        }

        # Add client credentials if configured
        if self.client_id and self.client_secret:
            data["client_id"] = self.client_id
            data["client_secret"] = self.client_secret

        try:
            response = await self.http_client.post(
                token_endpoint,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()

            result = response.json()

            access_token = result.get("access_token")
            expires_in = result.get("expires_in", 300)  # Default 5 minutes

            if not access_token:
                raise RuntimeError("No access token in refresh response")

            # Log that we're using fallback
            logger.warning(
                f"Using refresh grant fallback for token exchange. "
                f"Scopes: {requested_scopes}"
            )

            return access_token, expires_in

        except httpx.HTTPStatusError as e:
            logger.error(f"Refresh grant failed: {e.response.text}")
            raise RuntimeError(f"Refresh grant failed: {e}")
        except Exception as e:
            logger.error(f"Refresh grant error: {e}")
            raise


# Singleton instance
_token_exchange_service: Optional[TokenExchangeService] = None


async def get_token_exchange_service() -> TokenExchangeService:
    """Get or create the singleton token exchange service.

    Returns:
        TokenExchangeService instance
    """
    global _token_exchange_service

    if _token_exchange_service is None:
        _token_exchange_service = TokenExchangeService()
        await _token_exchange_service.storage.initialize()

    return _token_exchange_service


async def exchange_token_for_delegation(
    flow1_token: str, requested_scopes: list[str], requested_audience: str = "nextcloud"
) -> Tuple[str, int]:
    """Convenience function to exchange tokens.

    Args:
        flow1_token: The MCP session token (aud: "mcp-server")
        requested_scopes: Scopes needed for this operation
        requested_audience: Target audience (usually "nextcloud")

    Returns:
        Tuple of (delegated_token, expires_in)
    """
    service = await get_token_exchange_service()
    return await service.exchange_token_for_delegation(
        flow1_token=flow1_token,
        requested_scopes=requested_scopes,
        requested_audience=requested_audience,
    )
