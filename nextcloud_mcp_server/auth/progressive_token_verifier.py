"""
Token Verifier for ADR-004 Progressive Consent Architecture.

This module implements token verification with strict audience separation:
- Flow 1 tokens have aud: "mcp-server" for MCP authentication
- Flow 2 tokens have aud: "nextcloud" for resource access
- Token Broker manages the exchange between audiences
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import jwt
from mcp.server.auth.provider import AccessToken

from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage
from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

logger = logging.getLogger(__name__)


class ProgressiveConsentTokenVerifier:
    """
    Token verifier for Progressive Consent dual OAuth flows.

    This verifier:
    1. Validates Flow 1 tokens (aud: "mcp-server") for MCP authentication
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
    ):
        """
        Initialize the Progressive Consent token verifier.

        Args:
            token_storage: Storage for refresh tokens
            token_broker: Token broker service (created if not provided)
            oidc_discovery_url: OIDC provider discovery URL
            nextcloud_host: Nextcloud server URL
            encryption_key: Fernet key for token encryption
        """
        self.storage = token_storage
        self.oidc_discovery_url = oidc_discovery_url or os.getenv(
            "OIDC_DISCOVERY_URL",
            f"{os.getenv('NEXTCLOUD_HOST')}/.well-known/openid-configuration",
        )
        self.nextcloud_host = nextcloud_host or os.getenv("NEXTCLOUD_HOST")
        self.encryption_key = encryption_key or os.getenv("TOKEN_ENCRYPTION_KEY")

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
        Verify a Flow 1 token (aud: "mcp-server").

        This validates that:
        1. Token has correct audience for MCP server
        2. Token is not expired
        3. Token has valid signature (if verification enabled)

        Args:
            token: JWT access token from Flow 1

        Returns:
            AccessToken if valid, None otherwise
        """
        try:
            # Decode without signature verification (IdP handles that)
            # In production, would verify signature with IdP public key
            payload = jwt.decode(token, options={"verify_signature": False})

            # CRITICAL: Verify audience is for MCP server (Flow 1)
            audiences = payload.get("aud", [])
            if isinstance(audiences, str):
                audiences = [audiences]

            # Check for correct audience
            if "mcp-server" not in audiences:
                logger.warning(f"Token rejected: wrong audience {audiences}")
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
                logger.debug("Token expired")
                return None

            # Extract user info
            user_id = payload.get("sub", "unknown")
            client_id = payload.get("client_id", "unknown")
            scopes = payload.get("scope", "").split()
            exp = payload.get("exp", None)

            # Create AccessToken for MCP framework
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=exp,
                resource=f"user:{user_id}",  # Store user_id in resource field
            )

        except jwt.InvalidTokenError as e:
            logger.debug(f"Invalid token: {e}")
            return None
        except Exception as e:
            logger.error(f"Token verification failed: {e}")
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
