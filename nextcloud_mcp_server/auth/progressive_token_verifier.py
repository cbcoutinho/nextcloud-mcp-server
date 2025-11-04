"""
Token Verifier for ADR-004 Progressive Consent Architecture.

This module implements token verification with strict audience separation:
- Flow 1 tokens have aud: <mcp-client-id> for MCP authentication
- Flow 2 tokens have aud: "nextcloud" for resource access
- Token Broker manages the exchange between audiences
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage
from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

logger = logging.getLogger(__name__)


class ProgressiveConsentTokenVerifier:
    """
    Token verifier for Progressive Consent dual OAuth flows.

    This verifier:
    1. Validates Flow 1 tokens (aud: <mcp-client-id>) for MCP authentication
    2. Checks if user has provisioned Nextcloud access (Flow 2)
    3. Uses Token Broker to obtain aud: "nextcloud" tokens when needed
    """

    def __init__(
        self,
        token_storage: RefreshTokenStorage,
        token_broker: Optional[TokenBrokerService] = None,
        oidc_discovery_url: Optional[str] = None,
        nextcloud_host: Optional[str] = None,
        encryption_key: Optional[str] = None,
        mcp_client_id: Optional[str] = None,
        introspection_uri: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        """
        Initialize the Progressive Consent token verifier.

        Args:
            token_storage: Storage for refresh tokens
            token_broker: Token broker service (created if not provided)
            oidc_discovery_url: OIDC provider discovery URL
            nextcloud_host: Nextcloud server URL
            encryption_key: Fernet key for token encryption
            mcp_client_id: MCP server OAuth client ID for audience validation
            introspection_uri: OAuth introspection endpoint URL (for opaque tokens)
            client_secret: OAuth client secret (required for introspection)
        """
        self.storage = token_storage
        self.oidc_discovery_url = oidc_discovery_url or os.getenv(
            "OIDC_DISCOVERY_URL",
            f"{os.getenv('NEXTCLOUD_HOST')}/.well-known/openid-configuration",
        )
        self.nextcloud_host = nextcloud_host or os.getenv("NEXTCLOUD_HOST")
        self.encryption_key = encryption_key or os.getenv("TOKEN_ENCRYPTION_KEY")
        self.mcp_client_id = mcp_client_id or os.getenv("OIDC_CLIENT_ID")
        self.introspection_uri = introspection_uri
        self.client_secret = client_secret or os.getenv("OIDC_CLIENT_SECRET")

        # HTTP client for introspection requests
        self._http_client: Optional[httpx.AsyncClient] = None
        if self.introspection_uri and self.mcp_client_id and self.client_secret:
            self._http_client = httpx.AsyncClient(timeout=10.0)
            logger.info(f"Introspection support enabled: {introspection_uri}")
        elif self.introspection_uri:
            logger.warning(
                "Introspection URI provided but missing client credentials - introspection disabled"
            )

        # Create token broker if not provided
        if token_broker:
            self.token_broker = token_broker
        elif self.encryption_key:
            self.token_broker = TokenBrokerService(
                storage=token_storage,
                oidc_discovery_url=self.oidc_discovery_url,
                nextcloud_host=self.nextcloud_host,
                encryption_key=self.encryption_key,
            )
        else:
            self.token_broker = None
            logger.warning("Token broker not available - encryption key missing")

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """
        Verify a Flow 1 token (aud: <mcp-client-id>).

        This validates that:
        1. Token has correct audience for MCP server (matches client ID)
        2. Token is not expired
        3. Token has valid signature (if verification enabled)

        Supports both JWT and opaque tokens:
        - JWT tokens: Decoded directly from payload
        - Opaque tokens: Validated via introspection endpoint (RFC 7662)

        Args:
            token: Access token from Flow 1 (JWT or opaque)

        Returns:
            AccessToken if valid, None otherwise
        """
        logger.info("🔐 verify_token called - attempting to validate token")
        logger.info(f"Token (first 50 chars): {token[:50]}...")
        logger.info(f"Expected MCP client ID: {self.mcp_client_id}")

        # Check if token is JWT format (has 3 parts separated by dots)
        is_jwt = "." in token and token.count(".") == 2
        logger.info(f"Token format: {'JWT' if is_jwt else 'opaque'}")

        if is_jwt:
            # Try JWT verification
            return await self._verify_jwt_token(token)
        else:
            # Fall back to introspection for opaque tokens
            return await self._verify_opaque_token(token)

    async def _verify_jwt_token(self, token: str) -> Optional[AccessToken]:
        """Verify JWT token by decoding payload."""
        try:
            # Decode without signature verification (IdP handles that)
            # In production, would verify signature with IdP public key
            payload = jwt.decode(token, options={"verify_signature": False})
            logger.info(f"Token payload decoded: {payload}")

            # CRITICAL: Verify audience is for MCP server (Flow 1)
            audiences = payload.get("aud", [])
            if isinstance(audiences, str):
                audiences = [audiences]

            # Check for correct audience (must match MCP server client ID)
            if self.mcp_client_id not in audiences:
                logger.warning(
                    f"Token rejected: wrong audience {audiences}, expected {self.mcp_client_id}"
                )
                # Check if this is a Nextcloud token (wrong flow)
                if "nextcloud" in audiences:
                    logger.error(
                        "Received Nextcloud token in MCP context - "
                        "client may be using wrong token"
                    )
                return None

            # Check expiry
            exp = payload.get("exp", 0)
            if exp < datetime.now(timezone.utc).timestamp():
                logger.warning(
                    f"❌ Token expired: exp={exp}, now={datetime.now(timezone.utc).timestamp()}"
                )
                return None

            # Extract user info
            user_id = payload.get("sub", "unknown")
            client_id = payload.get("client_id", "unknown")
            scopes = payload.get("scope", "").split()
            exp = payload.get("exp", None)

            logger.info(
                f"✅ Token validation successful! user={user_id}, scopes={scopes}"
            )

            # Create AccessToken for MCP framework
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=exp,
                resource=user_id,  # Store user_id in resource field (RFC 8707)
            )

        except jwt.InvalidTokenError as e:
            logger.warning(f"❌ Invalid token (JWT decode failed): {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Token verification failed with exception: {e}")
            return None

    async def _verify_opaque_token(self, token: str) -> Optional[AccessToken]:
        """
        Verify opaque token via introspection endpoint (RFC 7662).

        Args:
            token: Opaque access token

        Returns:
            AccessToken if active and valid, None otherwise
        """
        if not self._http_client or not self.introspection_uri:
            logger.error(
                "❌ Cannot verify opaque token - introspection not configured. "
                "Set introspection_uri and client credentials."
            )
            return None

        try:
            logger.info(f"Introspecting token at {self.introspection_uri}")

            # Call introspection endpoint (requires client authentication)
            response = await self._http_client.post(
                self.introspection_uri,
                data={"token": token},
                auth=(self.mcp_client_id, self.client_secret),
            )

            if response.status_code != 200:
                logger.warning(
                    f"❌ Introspection failed: HTTP {response.status_code} - {response.text[:200]}"
                )
                return None

            introspection_data = response.json()
            logger.info(f"Introspection response: {introspection_data}")

            # Check if token is active
            if not introspection_data.get("active", False):
                logger.warning("❌ Token introspection returned active=false")
                return None

            # Extract user info
            user_id = introspection_data.get("sub") or introspection_data.get(
                "username"
            )
            if not user_id:
                logger.error("❌ No username found in introspection response")
                return None

            # Extract scopes (space-separated string)
            scope_string = introspection_data.get("scope", "")
            scopes = scope_string.split() if scope_string else []

            # Extract client ID and expiration
            client_id = introspection_data.get("client_id", "unknown")
            exp = introspection_data.get("exp")

            logger.info(f"✅ Opaque token validated! user={user_id}, scopes={scopes}")

            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(exp) if exp else None,
                resource=user_id,
            )

        except httpx.TimeoutException:
            logger.error("❌ Timeout while introspecting token")
            return None
        except httpx.RequestError as e:
            logger.error(f"❌ Network error during introspection: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Introspection failed with exception: {e}")
            return None

    async def check_provisioning(self, user_id: str) -> bool:
        """
        Check if user has provisioned Nextcloud access (Flow 2).

        Args:
            user_id: User identifier from Flow 1 token

        Returns:
            True if user has completed Flow 2, False otherwise
        """
        if not self.storage:
            return False

        refresh_data = await self.storage.get_refresh_token(user_id)
        return refresh_data is not None

    async def get_nextcloud_token(self, user_id: str) -> Optional[str]:
        """
        Get a Nextcloud access token (aud: "nextcloud") for the user.

        This uses the Token Broker to:
        1. Check for cached Nextcloud token
        2. If expired, refresh using stored master refresh token
        3. Return token with aud: "nextcloud" for API access

        Args:
            user_id: User identifier from Flow 1 token

        Returns:
            Nextcloud access token if provisioned, None otherwise
        """
        if not self.token_broker:
            logger.error("Token broker not available")
            return None

        # Check if user has provisioned access
        if not await self.check_provisioning(user_id):
            logger.info(f"User {user_id} has not provisioned Nextcloud access")
            return None

        # Get or refresh Nextcloud token
        try:
            nextcloud_token = await self.token_broker.get_nextcloud_token(user_id)
            if nextcloud_token:
                logger.debug(f"Obtained Nextcloud token for user {user_id}")
            return nextcloud_token
        except Exception as e:
            logger.error(f"Failed to get Nextcloud token: {e}")
            return None

    async def validate_scopes(
        self, token: AccessToken, required_scopes: list[str]
    ) -> bool:
        """
        Validate that token has required scopes.

        Args:
            token: The access token
            required_scopes: List of required scopes

        Returns:
            True if all required scopes present, False otherwise
        """
        token_scopes = set(token.scopes) if token.scopes else set()
        required = set(required_scopes)

        missing = required - token_scopes
        if missing:
            logger.debug(f"Token missing required scopes: {missing}")
            return False

        return True

    async def close(self):
        """Clean up resources."""
        if self.token_broker:
            await self.token_broker.close()
        if self._http_client:
            await self._http_client.aclose()
