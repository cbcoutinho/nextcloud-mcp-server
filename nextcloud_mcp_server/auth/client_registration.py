"""Dynamic client registration for Nextcloud OIDC."""

import datetime as dt
import logging
import time
from typing import Any

import anyio
import httpx

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

logger = logging.getLogger(__name__)


class ClientInfo:
    """Client registration information with RFC 7592 support."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        client_id_issued_at: int,
        client_secret_expires_at: int,
        redirect_uris: list[str],
        registration_access_token: str | None = None,
        registration_client_uri: str | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.client_id_issued_at = client_id_issued_at
        self.client_secret_expires_at = client_secret_expires_at
        self.redirect_uris = redirect_uris
        self.registration_access_token = registration_access_token
        self.registration_client_uri = registration_client_uri

    @property
    def is_expired(self) -> bool:
        """Check if the client has expired."""
        return time.time() >= self.client_secret_expires_at

    @property
    def expires_soon(self) -> bool:
        """Check if client expires within 5 minutes."""
        return time.time() >= (self.client_secret_expires_at - 300)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage."""
        result = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "client_id_issued_at": self.client_id_issued_at,
            "client_secret_expires_at": self.client_secret_expires_at,
            "redirect_uris": self.redirect_uris,
        }
        if self.registration_access_token:
            result["registration_access_token"] = self.registration_access_token
        if self.registration_client_uri:
            result["registration_client_uri"] = self.registration_client_uri
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClientInfo":
        """Create from dictionary."""
        return cls(
            client_id=data["client_id"],
            client_secret=data["client_secret"],
            client_id_issued_at=data["client_id_issued_at"],
            client_secret_expires_at=data["client_secret_expires_at"],
            redirect_uris=data["redirect_uris"],
            registration_access_token=data.get("registration_access_token"),
            registration_client_uri=data.get("registration_client_uri"),
        )


async def register_client(
    nextcloud_url: str,
    registration_endpoint: str,
    client_name: str = "Nextcloud MCP Server",
    redirect_uris: list[str] | None = None,
    scopes: str = "openid profile email",
    token_type: str | None = "Bearer",
    resource_url: str | None = None,
) -> ClientInfo:
    """
    Register a new OAuth client using RFC 7591 Dynamic Client Registration.

    This function supports both Nextcloud OIDC and standard OIDC providers like Keycloak.

    Args:
        nextcloud_url: Base URL of the OIDC provider
        registration_endpoint: Full URL to the registration endpoint
        client_name: Name of the client application
        redirect_uris: List of redirect URIs (default: http://localhost:8000/oauth/callback)
        scopes: Space-separated list of scopes to request
        token_type: Type of access tokens (default: "Bearer", supports "JWT" for Nextcloud).
                    Set to None to omit this field (required for Keycloak and other standard providers).
        resource_url: OAuth 2.0 Protected Resource URL (RFC 9728) - used for token introspection authorization

    Returns:
        ClientInfo with registration details

    Raises:
        httpx.HTTPStatusError: If registration fails
        ValueError: If response is invalid

    Note:
        The token_type parameter is a Nextcloud-specific extension and is not part of RFC 7591.
        Standard OIDC providers like Keycloak do not accept this field and will return a 400 error
        if it's included. Set token_type=None when registering with Keycloak or other standard providers.
    """
    if redirect_uris is None:
        redirect_uris = ["http://localhost:8000/oauth/callback"]

    client_metadata = {
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": scopes,
    }

    # Add token_type if provided (Nextcloud-specific, not RFC 7591 standard)
    if token_type is not None:
        client_metadata["token_type"] = token_type

    # Add resource_url if provided (RFC 9728)
    if resource_url:
        client_metadata["resource_url"] = resource_url

    logger.info(f"Registering OAuth client with Nextcloud: {client_name}")
    logger.debug(f"Registration endpoint: {registration_endpoint}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                registration_endpoint,
                json=client_metadata,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()

            client_info = response.json()
            logger.info(
                f"Successfully registered client: {client_info.get('client_id')}"
            )
            expires_at = dt.datetime.fromtimestamp(
                client_info.get("client_secret_expires_at")
            )
            logger.info(
                f"Client expires at: {expires_at} "
                f"(in {client_info.get('client_secret_expires_at', 0) - int(time.time())} seconds)"
            )

            # Log if RFC 7592 fields are present
            has_reg_token = "registration_access_token" in client_info
            has_reg_uri = "registration_client_uri" in client_info
            if has_reg_token and has_reg_uri:
                logger.info(
                    "RFC 7592 management fields received - client deletion will be supported"
                )
            else:
                logger.warning("RFC 7592 fields missing - client deletion may not work")

            return ClientInfo(
                client_id=client_info["client_id"],
                client_secret=client_info["client_secret"],
                client_id_issued_at=client_info.get(
                    "client_id_issued_at", int(time.time())
                ),
                client_secret_expires_at=client_info.get(
                    "client_secret_expires_at", int(time.time()) + 3600
                ),
                redirect_uris=client_info.get("redirect_uris", redirect_uris),
                registration_access_token=client_info.get("registration_access_token"),
                registration_client_uri=client_info.get("registration_client_uri"),
            )

        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to register client: HTTP {e.response.status_code}")
            logger.error(f"Response: {e.response.text}")
            raise
        except KeyError as e:
            logger.error(f"Invalid response from registration endpoint: missing {e}")
            raise ValueError(f"Invalid registration response: missing {e}")


async def delete_client(
    nextcloud_url: str,
    client_id: str,
    registration_access_token: str | None = None,
    client_secret: str | None = None,
    registration_client_uri: str | None = None,
    max_retries: int = 3,
) -> bool:
    """
    Delete a dynamically registered OAuth client using RFC 7592.

    This implements RFC 7592 Section 2.3 (Client Delete Request).
    Prefers Bearer token authentication (RFC 7592 standard) but falls back
    to HTTP Basic Auth if registration_access_token is not available.

    Args:
        nextcloud_url: Base URL of the Nextcloud instance
        client_id: Client identifier to delete
        registration_access_token: RFC 7592 registration access token (preferred)
        client_secret: Client secret for fallback HTTP Basic Auth
        registration_client_uri: RFC 7592 client configuration URI (optional)
        max_retries: Maximum number of retries for 429 responses (default: 3)

    Returns:
        True if deletion successful, False otherwise

    Note:
        RFC 7592 deletion endpoint: {registration_client_uri} or {nextcloud_url}/apps/oidc/register/{client_id}

        Authentication methods (in order of preference):
        1. Bearer token: Authorization: Bearer {registration_access_token} (RFC 7592 standard)
        2. HTTP Basic Auth: client_id as username, client_secret as password (fallback)
    """

    # Determine deletion endpoint
    if registration_client_uri:
        deletion_endpoint = registration_client_uri
    else:
        deletion_endpoint = f"{nextcloud_url}/apps/oidc/register/{client_id}"

    logger.info(f"Deleting OAuth client: {client_id[:16]}...")
    logger.debug(f"Deletion endpoint: {deletion_endpoint}")

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        for attempt in range(max_retries):
            try:
                # Prefer RFC 7592 Bearer token authentication
                if registration_access_token:
                    logger.debug("Using RFC 7592 Bearer token authentication")
                    response = await http_client.delete(
                        deletion_endpoint,
                        headers={
                            "Authorization": f"Bearer {registration_access_token}"
                        },
                    )
                elif client_secret:
                    logger.debug(
                        "Falling back to HTTP Basic Auth (registration_access_token not available)"
                    )
                    response = await http_client.delete(
                        deletion_endpoint,
                        auth=(client_id, client_secret),
                    )
                else:
                    logger.error(
                        "Cannot delete client: no registration_access_token or client_secret provided"
                    )
                    return False

                # RFC 7592: Successful deletion returns 204 No Content
                if response.status_code == 204:
                    logger.info(
                        f"Successfully deleted OAuth client: {client_id[:16]}..."
                    )
                    return True
                elif response.status_code == 429:
                    # Rate limited - retry with exponential backoff
                    if attempt < max_retries - 1:
                        retry_after = int(response.headers.get("Retry-After", 2))
                        wait_time = min(
                            retry_after, 2**attempt
                        )  # Exponential backoff, max from header
                        logger.warning(
                            f"Rate limited (429) deleting client {client_id[:16]}..., "
                            f"retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                        )
                        await anyio.sleep(wait_time)
                        continue
                    else:
                        logger.error(
                            f"Failed to delete client {client_id[:16]}... after {max_retries} attempts: Rate limited (429)"
                        )
                        return False
                elif response.status_code == 401:
                    logger.error(
                        f"Failed to delete client {client_id[:16]}...: Authentication failed (invalid credentials)"
                    )
                    return False
                elif response.status_code == 403:
                    logger.error(
                        f"Failed to delete client {client_id[:16]}...: Not authorized (not a DCR client or wrong client)"
                    )
                    return False
                else:
                    logger.error(
                        f"Failed to delete client {client_id[:16]}...: HTTP {response.status_code}"
                    )
                    logger.debug(f"Response: {response.text}")
                    return False

            except httpx.HTTPStatusError as e:
                logger.error(
                    f"HTTP error deleting client {client_id[:16]}...: {e.response.status_code}"
                )
                logger.debug(f"Response: {e.response.text}")
                return False
            except Exception as e:
                logger.error(
                    f"Unexpected error deleting client {client_id[:16]}...: {e}"
                )
                return False

        # Should not reach here, but return False if we do
        return False


async def ensure_oauth_client(
    nextcloud_url: str,
    registration_endpoint: str,
    storage: RefreshTokenStorage,
    client_name: str = "Nextcloud MCP Server",
    redirect_uris: list[str] | None = None,
    scopes: str = "openid profile email",
    token_type: str = "Bearer",
    resource_url: str | None = None,
) -> ClientInfo:
    """
    Ensure OAuth client exists in SQLite storage.

    This function:
    1. Checks for existing client credentials in SQLite storage
    2. Validates the credentials are not expired
    3. Registers a new client if needed (no stored credentials or expired)
    4. Saves the new client credentials to SQLite

    Args:
        nextcloud_url: Base URL of the Nextcloud instance
        registration_endpoint: Full URL to the registration endpoint
        storage: RefreshTokenStorage instance for SQLite storage
        client_name: Name of the client application
        redirect_uris: List of redirect URIs
        scopes: Space-separated list of scopes to request (default: "openid profile email")
        token_type: Type of access tokens to issue (default: "Bearer", also supports "JWT")
        resource_url: OAuth 2.0 Protected Resource URL (RFC 9728) - used for token introspection authorization

    Returns:
        ClientInfo with valid credentials

    Raises:
        httpx.HTTPStatusError: If registration fails
        ValueError: If response is invalid
    """
    # Try to load existing client from SQLite
    client_data = await storage.get_oauth_client()
    if client_data:
        logger.info(
            f"Loaded OAuth client from SQLite: {client_data['client_id'][:16]}..."
        )
        return ClientInfo.from_dict(client_data)

    # Register new client
    logger.info("Registering new OAuth client...")
    if resource_url:
        logger.info(f"  with resource_url: {resource_url}")
    client_info = await register_client(
        nextcloud_url=nextcloud_url,
        registration_endpoint=registration_endpoint,
        client_name=client_name,
        redirect_uris=redirect_uris,
        scopes=scopes,
        token_type=token_type,
        resource_url=resource_url,
    )

    # Save to SQLite storage
    await storage.store_oauth_client(
        client_id=client_info.client_id,
        client_secret=client_info.client_secret,
        client_id_issued_at=client_info.client_id_issued_at,
        client_secret_expires_at=client_info.client_secret_expires_at,
        redirect_uris=client_info.redirect_uris,
        registration_access_token=client_info.registration_access_token,
        registration_client_uri=client_info.registration_client_uri,
    )

    return client_info
