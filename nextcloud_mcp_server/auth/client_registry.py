"""
MCP Client Registry for ADR-004 Progressive Consent Architecture.

This module manages the registry of allowed MCP clients that can authenticate
via Flow 1. In production, this would integrate with Dynamic Client Registration
(DCR) or a database of pre-registered clients.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse

from nextcloud_mcp_server.config import cfg

logger = logging.getLogger(__name__)


@dataclass
class MCPClientInfo:
    """Information about a registered MCP client."""

    client_id: str
    name: str
    redirect_uris: List[str]
    allowed_scopes: List[str]
    is_public: bool = True  # Native clients are public (no client_secret)
    metadata: Optional[Dict] = None


class ClientRegistry:
    """
    Registry for MCP clients allowed to authenticate via Flow 1.

    In production, this would:
    1. Support Dynamic Client Registration (DCR) per RFC 7591
    2. Integrate with IdP client registry
    3. Store client metadata in database
    4. Support client updates and revocation

    Scope Policy:
        All clients are registered with allowed_scopes=["*"] (wildcard).
        The MCP server acts as an OAuth AS proxy — it validates client
        identity and redirect URIs locally, but delegates scope enforcement
        to the upstream IdP (Nextcloud or Keycloak).
    """

    def __init__(self, allow_dynamic_registration: bool = False):
        """
        Initialize the client registry.

        Args:
            allow_dynamic_registration: Whether to allow DCR for new clients
        """
        self.allow_dynamic_registration = allow_dynamic_registration
        self._clients: Dict[str, MCPClientInfo] = {}
        self._load_static_clients()

    def _load_static_clients(self):
        """Load statically configured clients from environment.

        Format: comma-separated entries, each either:
        - Simple client ID: gets localhost redirect URIs
        - client_id|redirect_uri: gets the specified redirect URI

        Redirect URI rules:
        - http://localhost:* and http://127.0.0.1:* are allowed (native clients)
        - https:// redirect URIs are allowed (cloud clients)
        - http:// non-localhost redirect URIs are rejected with a warning

        If the env var is unset or empty, the registry remains empty and
        ``validate_client`` rejects every client_id (fail-closed). There are no
        built-in defaults; operators must opt-in clients explicitly.
        """
        # NOTE: ALLOWED_MCP_CLIENTS and ALLOWED_MGMT_CLIENT are currently separate
        # env vars to keep the MCP-route and management-API auth surfaces
        # independent. These may be consolidated into a single env var later
        # once the deployment story stabilises.
        allowed_clients = cfg("ALLOWED_MCP_CLIENTS", "").strip()

        if allowed_clients:
            for entry in allowed_clients.split(","):
                entry = entry.strip()
                if not entry:
                    continue

                if "|" in entry:
                    cid, redirect = entry.split("|", 1)
                    cid, redirect = cid.strip(), redirect.strip()

                    if not cid or not redirect:
                        logger.warning(
                            "Skipping malformed ALLOWED_MCP_CLIENTS entry: %r", entry
                        )
                        continue

                    parsed = urlparse(redirect)
                    hostname = parsed.hostname
                    if hostname is None:
                        logger.warning(
                            "Skipping client %r: cannot parse hostname from %r",
                            cid,
                            redirect,
                        )
                        continue
                    is_loopback = hostname in ("localhost", "127.0.0.1", "::1")

                    if not (
                        parsed.scheme == "https"
                        or (parsed.scheme == "http" and is_loopback)
                    ):
                        logger.warning(
                            "Rejecting client %r: HTTP redirect URIs are only allowed for localhost, got %r",
                            cid,
                            redirect,
                        )
                        continue

                    self._clients[cid] = MCPClientInfo(
                        client_id=cid,
                        name=self._get_client_name(cid),
                        redirect_uris=[redirect],
                        allowed_scopes=["*"],
                        is_public=True,
                    )
                    logger.info("Registered static client: %s", cid)
                else:
                    self._clients[entry] = MCPClientInfo(
                        client_id=entry,
                        name=self._get_client_name(entry),
                        redirect_uris=["http://localhost:*", "http://127.0.0.1:*"],
                        allowed_scopes=["*"],
                        is_public=True,
                    )
                    logger.info("Registered static client: %s", entry)

        if not self._clients:
            logger.warning(
                "Client registry is empty: ALLOWED_MCP_CLIENTS is unset or empty. "
                "All MCP-flow OAuth requests will be rejected until configured."
            )

    def _get_client_name(self, client_id: str) -> str:
        """Derive a human-readable display name from a client_id.

        There is no built-in list of "well-known" clients: every client must be
        opted in explicitly via ``ALLOWED_MCP_CLIENTS`` (mirroring the
        management-API ``ALLOWED_MGMT_CLIENT`` allowlist). The display name is
        derived generically from the client_id.
        """
        return client_id.replace("-", " ").title()

    def validate_client(
        self,
        client_id: str,
        redirect_uri: Optional[str] = None,
        scopes: Optional[List[str]] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Validate a client_id and optionally its redirect_uri and scopes.

        Args:
            client_id: The client identifier to validate
            redirect_uri: Optional redirect URI to validate
            scopes: Optional list of scopes to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check if client exists
        client = self._clients.get(client_id)
        if not client:
            if self.allow_dynamic_registration:
                # In production, would attempt DCR here
                logger.info("Unknown client %s, would attempt DCR", client_id)
                return True, None
            else:
                return False, f"Unknown client_id: {client_id}"

        # Validate redirect_uri if provided
        if redirect_uri:
            if not self._validate_redirect_uri(client, redirect_uri):
                return False, f"Invalid redirect_uri for client {client_id}"

        # Validate scopes if provided (wildcard "*" allows all scopes)
        if scopes and "*" not in client.allowed_scopes:
            invalid_scopes = set(scopes) - set(client.allowed_scopes)
            if invalid_scopes:
                return False, f"Invalid scopes for client {client_id}: {invalid_scopes}"

        return True, None

    def _validate_redirect_uri(self, client: MCPClientInfo, redirect_uri: str) -> bool:
        """
        Validate redirect_uri against client's registered URIs.

        Args:
            client: The client info
            redirect_uri: The URI to validate

        Returns:
            True if valid, False otherwise
        """
        # Parse the redirect URI
        parsed = urlparse(redirect_uri)
        if not parsed.hostname:
            return False

        # Check against registered patterns
        for pattern in client.redirect_uris:
            if "*" in pattern:
                # Handle wildcard port (localhost:*)
                pattern_base = pattern.replace(":*", "")
                if redirect_uri.startswith(pattern_base + ":"):
                    # Validate it's localhost with a port
                    if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
                        return True
            elif redirect_uri == pattern:
                return True

        return False

    def register_client(self, client_info: MCPClientInfo) -> bool:
        """
        Register a new MCP client (DCR support).

        Args:
            client_info: Client information to register

        Returns:
            True if registered successfully
        """
        if not self.allow_dynamic_registration:
            logger.warning("DCR disabled, cannot register %s", client_info.client_id)
            return False

        if client_info.client_id in self._clients:
            logger.warning("Client %s already registered", client_info.client_id)
            return False

        self._clients[client_info.client_id] = client_info
        logger.info("Dynamically registered client: %s", client_info.client_id)

        # In production, would persist to database
        return True

    def register_proxy_client(
        self, client_id: str, redirect_uris: list[str], name: str = ""
    ) -> None:
        """Register a client discovered via DCR proxy.

        When the MCP server acts as an OAuth AS proxy, clients register via
        the proxy's /oauth/register endpoint. This method stores the client
        locally so /oauth/authorize can validate it.

        Args:
            client_id: Client identifier from Nextcloud DCR response
            redirect_uris: Allowed redirect URIs
            name: Optional human-readable name
        """
        self._clients[client_id] = MCPClientInfo(
            client_id=client_id,
            name=name or f"DCR-{client_id[:8]}",
            redirect_uris=redirect_uris or ["http://localhost:*", "http://127.0.0.1:*"],
            allowed_scopes=["*"],  # Nextcloud enforces actual scopes
            is_public=True,
        )
        logger.info("Registered proxy client: %s", client_id)

    def get_client(self, client_id: str) -> Optional[MCPClientInfo]:
        """
        Get client information.

        Args:
            client_id: The client identifier

        Returns:
            Client info if found, None otherwise
        """
        return self._clients.get(client_id)

    def list_clients(self) -> List[MCPClientInfo]:
        """
        List all registered clients.

        Returns:
            List of client information
        """
        return list(self._clients.values())


# Global registry instance
_registry: Optional[ClientRegistry] = None


def get_client_registry() -> ClientRegistry:
    """Get the global client registry instance."""
    global _registry
    if _registry is None:
        # Check if DCR is enabled. str() wraps it because dynaconf type-coerces
        # env "true" -> bool True (which has no .lower()).
        allow_dcr = str(cfg("ENABLE_DCR", "false")).lower() == "true"
        _registry = ClientRegistry(allow_dynamic_registration=allow_dcr)
    return _registry
