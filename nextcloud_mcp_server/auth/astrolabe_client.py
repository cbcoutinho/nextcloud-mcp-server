"""
Client for querying Astrolabe Management API for background sync credentials.

This client uses OAuth client credentials flow to authenticate to Nextcloud
and retrieve user app passwords for background sync operations.
"""

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class AstrolabeClient:
    """Client for querying Astrolabe API for background sync credentials.

    Uses OAuth client credentials flow to authenticate as the MCP server
    and retrieve user app passwords that are stored in Nextcloud.
    """

    def __init__(
        self,
        nextcloud_host: str,
        client_id: str,
        client_secret: str,
    ):
        """
        Initialize Astrolabe client.

        Args:
            nextcloud_host: Nextcloud base URL (e.g., https://cloud.example.com)
            client_id: OAuth client ID for MCP server
            client_secret: OAuth client secret
        """
        self.nextcloud_host = nextcloud_host.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._token_cache: Optional[dict] = None  # {access_token, expires_at}

    async def get_access_token(self) -> str:
        """
        Get access token using OAuth client credentials flow.

        Tokens are cached with 1-minute early refresh to avoid expiration.

        Returns:
            Access token string

        Raises:
            httpx.HTTPError: If token request fails
        """
        # Check cache
        if self._token_cache and time.time() < self._token_cache["expires_at"]:
            logger.debug("Using cached OAuth token for Astrolabe API")
            return self._token_cache["access_token"]

        # Discover token endpoint
        discovery_url = f"{self.nextcloud_host}/.well-known/openid-configuration"

        async with httpx.AsyncClient() as client:
            logger.debug(f"Discovering token endpoint from {discovery_url}")
            discovery_resp = await client.get(discovery_url)
            discovery_resp.raise_for_status()
            token_endpoint = discovery_resp.json()["token_endpoint"]

            logger.debug(f"Requesting client credentials token from {token_endpoint}")

            # Request token using client credentials grant
            token_resp = await client.post(
                token_endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "openid",  # Minimal scope
                },
            )
            token_resp.raise_for_status()
            data = token_resp.json()

            # Cache with 1-minute early refresh
            expires_in = data.get("expires_in", 3600)
            self._token_cache = {
                "access_token": data["access_token"],
                "expires_at": time.time() + expires_in - 60,
            }

            logger.info(f"Obtained Astrolabe API token (expires in {expires_in}s)")
            return data["access_token"]

    async def get_user_app_password(self, user_id: str) -> Optional[str]:
        """
        Retrieve user's app password for background sync.

        Args:
            user_id: Nextcloud user ID

        Returns:
            App password string, or None if user hasn't provisioned

        Raises:
            httpx.HTTPError: If API request fails (except 404)
        """
        token = await self.get_access_token()
        url = f"{self.nextcloud_host}/apps/astrolabe/api/v1/background-sync/credentials/{user_id}"

        async with httpx.AsyncClient() as client:
            logger.debug(f"Retrieving app password for user: {user_id}")

            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )

            if response.status_code == 404:
                logger.debug(f"No app password configured for user: {user_id}")
                return None

            response.raise_for_status()
            data = response.json()

            logger.info(
                f"Retrieved app password for user: {user_id} (type: {data.get('credential_type')})"
            )
            return data.get("app_password")

    async def get_background_sync_status(self, user_id: str) -> dict:
        """
        Get background sync status for a user.

        Args:
            user_id: Nextcloud user ID

        Returns:
            Dict with keys: has_access, credential_type, provisioned_at

        Raises:
            httpx.HTTPError: If API request fails
        """
        # For now, check if app password exists
        # In the future, this could query a dedicated status endpoint
        app_password = await self.get_user_app_password(user_id)

        return {
            "has_access": app_password is not None,
            "credential_type": "app_password" if app_password else None,
            "provisioned_at": None,  # TODO: Get from API if available
        }
