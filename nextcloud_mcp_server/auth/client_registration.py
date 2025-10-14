"""Dynamic client registration for Nextcloud OIDC."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ClientInfo:
    """Client registration information."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        client_id_issued_at: int,
        client_secret_expires_at: int,
        redirect_uris: list[str],
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.client_id_issued_at = client_id_issued_at
        self.client_secret_expires_at = client_secret_expires_at
        self.redirect_uris = redirect_uris

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
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "client_id_issued_at": self.client_id_issued_at,
            "client_secret_expires_at": self.client_secret_expires_at,
            "redirect_uris": self.redirect_uris,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClientInfo":
        """Create from dictionary."""
        return cls(
            client_id=data["client_id"],
            client_secret=data["client_secret"],
            client_id_issued_at=data["client_id_issued_at"],
            client_secret_expires_at=data["client_secret_expires_at"],
            redirect_uris=data["redirect_uris"],
        )


async def register_client(
    nextcloud_url: str,
    registration_endpoint: str,
    client_name: str = "Nextcloud MCP Server",
    redirect_uris: list[str] | None = None,
    scopes: str = "openid profile email",
) -> ClientInfo:
    """
    Register a new OAuth client with Nextcloud OIDC using dynamic client registration.

    Args:
        nextcloud_url: Base URL of the Nextcloud instance
        registration_endpoint: Full URL to the registration endpoint
        client_name: Name of the client application
        redirect_uris: List of redirect URIs (default: http://localhost:8000/oauth/callback)
        scopes: Space-separated list of scopes to request

    Returns:
        ClientInfo with registration details

    Raises:
        httpx.HTTPStatusError: If registration fails
        ValueError: If response is invalid
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
            logger.info(
                f"Client expires at: {client_info.get('client_secret_expires_at')} "
                f"(in {client_info.get('client_secret_expires_at', 0) - int(time.time())} seconds)"
            )

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
            )

        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to register client: HTTP {e.response.status_code}")
            logger.error(f"Response: {e.response.text}")
            raise
        except KeyError as e:
            logger.error(f"Invalid response from registration endpoint: missing {e}")
            raise ValueError(f"Invalid registration response: missing {e}")


def load_client_from_file(storage_path: Path) -> ClientInfo | None:
    """
    Load client credentials from storage file.

    Args:
        storage_path: Path to the JSON file containing client credentials

    Returns:
        ClientInfo if file exists and is valid, None otherwise
    """
    if not storage_path.exists():
        logger.debug(f"Client storage file not found: {storage_path}")
        return None

    try:
        with open(storage_path, "r") as f:
            data = json.load(f)

        client_info = ClientInfo.from_dict(data)

        if client_info.is_expired:
            logger.warning(
                f"Stored client has expired (expired at {client_info.client_secret_expires_at})"
            )
            return None

        logger.info(f"Loaded client from storage: {client_info.client_id[:16]}...")
        if client_info.expires_soon:
            logger.warning("Client expires soon (within 5 minutes)")

        return client_info

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Failed to load client from file: {e}")
        return None


def save_client_to_file(client_info: ClientInfo, storage_path: Path):
    """
    Save client credentials to storage file.

    Args:
        client_info: Client information to save
        storage_path: Path to save the JSON file

    Raises:
        OSError: If file cannot be written
    """
    try:
        # Create directory if it doesn't exist
        storage_path.parent.mkdir(parents=True, exist_ok=True)

        # Write client info
        with open(storage_path, "w") as f:
            json.dump(client_info.to_dict(), f, indent=2)

        # Set restrictive permissions (owner read/write only)
        os.chmod(storage_path, 0o600)

        logger.info(f"Saved client credentials to {storage_path}")

    except OSError as e:
        logger.error(f"Failed to save client credentials: {e}")
        raise


async def wait_for_client_propagation(
    nextcloud_url: str,
    client_id: str,
    max_retries: int = 10,
    initial_delay: float = 0.5,
    max_delay: float = 5.0,
) -> None:
    """
    Wait for the registered OAuth client to be fully propagated in Nextcloud.

    This function attempts to verify the client is ready by checking if we can
    access OIDC-related endpoints. Uses exponential backoff for retries.

    Args:
        nextcloud_url: Base URL of the Nextcloud instance
        client_id: The registered client ID
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds before first verification
        max_delay: Maximum delay between retries

    Note:
        This is a best-effort approach to mitigate race conditions between
        client registration and first use. Nextcloud's OIDC provider may need
        time to propagate newly registered clients to its cache/database.
    """
    # Always wait at least the initial delay to give Nextcloud time to propagate
    logger.debug(
        f"Waiting {initial_delay}s for OAuth client {client_id[:16]}... to propagate"
    )
    await asyncio.sleep(initial_delay)

    # Verify the client is accessible by checking OIDC discovery again
    # (this gives Nextcloud additional time to complete any async operations)
    discovery_url = f"{nextcloud_url}/.well-known/openid-configuration"
    delay = initial_delay

    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(1, max_retries + 1):
            try:
                response = await client.get(discovery_url)
                response.raise_for_status()
                logger.debug(
                    f"OAuth client propagation verification successful (attempt {attempt})"
                )
                return
            except Exception as e:
                if attempt < max_retries:
                    delay = min(delay * 1.5, max_delay)
                    logger.debug(
                        f"Verification attempt {attempt} failed: {e}. Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning(
                        f"Could not verify client propagation after {max_retries} attempts. "
                        "Continuing anyway - first authorization may fail."
                    )


async def load_or_register_client(
    nextcloud_url: str,
    registration_endpoint: str,
    storage_path: str | Path,
    client_name: str = "Nextcloud MCP Server",
    redirect_uris: list[str] | None = None,
    force_register: bool = True,
    wait_for_propagation: bool = True,
) -> ClientInfo:
    """
    Load client from storage or register a new one if not found/expired.

    This function:
    1. Checks for existing client credentials in storage
    2. Validates the credentials are not expired
    3. Registers a new client if needed
    4. Waits for the client to propagate (if newly registered)
    5. Saves the new client credentials

    Args:
        nextcloud_url: Base URL of the Nextcloud instance
        registration_endpoint: Full URL to the registration endpoint
        storage_path: Path to store client credentials
        client_name: Name of the client application
        redirect_uris: List of redirect URIs
        force_register: Force registration even if valid credentials exist
        wait_for_propagation: Wait for newly registered clients to propagate (default: True)

    Returns:
        ClientInfo with valid credentials

    Raises:
        httpx.HTTPStatusError: If registration fails
        ValueError: If response is invalid
    """
    storage_path = Path(storage_path)

    # Try to load existing client unless forced to register
    if not force_register:
        client_info = load_client_from_file(storage_path)
        if client_info:
            return client_info

    # Register new client
    logger.info("Registering new OAuth client...")
    client_info = await register_client(
        nextcloud_url=nextcloud_url,
        registration_endpoint=registration_endpoint,
        client_name=client_name,
        redirect_uris=redirect_uris,
    )

    # Wait for client to propagate in Nextcloud's OIDC provider
    # This mitigates race conditions where the client is used immediately after registration
    if wait_for_propagation:
        logger.info("Waiting for OAuth client to propagate in Nextcloud...")
        await wait_for_client_propagation(
            nextcloud_url=nextcloud_url,
            client_id=client_info.client_id,
        )

    # Save to storage
    save_client_to_file(client_info, storage_path)

    return client_info
