"""
Client for querying Astrolabe Management API for background sync credentials.

This client uses OAuth client credentials flow to authenticate to Nextcloud
and retrieve user app passwords for background sync operations.
"""

import logging
import time

from ..http import nextcloud_httpx_client

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
        self._token_cache: dict | None = None  # {access_token, expires_at}

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

        async with nextcloud_httpx_client() as client:
            logger.debug("Discovering token endpoint from %s", discovery_url)
            discovery_resp = await client.get(discovery_url)
            discovery_resp.raise_for_status()
            token_endpoint = discovery_resp.json()["token_endpoint"]

            logger.debug("Requesting client credentials token from %s", token_endpoint)

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

            logger.info("Obtained Astrolabe API token (expires in %ss)", expires_in)
            return data["access_token"]

    async def get_background_sync_status(self, user_id: str) -> dict:
        """
        Get background sync provisioning status for a user.

        Queries Astrolabe's admin credentials-metadata endpoint, which returns
        presence/timestamps only — never the app password itself. The password
        is delivered to the MCP server out-of-band (Astrolabe pushes it to
        ``POST /api/v1/users/{user_id}/app-password``), so this endpoint exposes
        only ``has_background_access`` / ``sync_type`` / ``provisioned_at``.

        Args:
            user_id: Nextcloud user ID

        Returns:
            Dict with keys: has_access (bool), credential_type (str | None),
            provisioned_at (int | None) — Unix seconds, not an ISO string.

        Raises:
            httpx.HTTPError: If the API request fails (except 404, treated as
            "not provisioned").
        """
        token = await self.get_access_token()
        url = f"{self.nextcloud_host}/apps/astrolabe/api/v1/background-sync/credentials/{user_id}"

        async with nextcloud_httpx_client() as client:
            logger.debug("Fetching background-sync status for user: %s", user_id)

            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )

            if response.status_code == 404:
                logger.debug("No background-sync credentials for user: %s", user_id)
                return {
                    "has_access": False,
                    "credential_type": None,
                    "provisioned_at": None,
                }

            response.raise_for_status()
            data = response.json()

            has_access = bool(data.get("has_background_access"))
            logger.info(
                "Background-sync status for user %s: has_access=%s",
                user_id,
                has_access,
            )
            return {
                "has_access": has_access,
                "credential_type": data.get("sync_type"),
                "provisioned_at": data.get("provisioned_at"),
            }
