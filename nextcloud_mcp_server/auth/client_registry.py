"""
MCP Client Registry for ADR-004 Progressive Consent Architecture.

This module manages the registry of allowed MCP clients that can authenticate
via Flow 1. In production, this would integrate with Dynamic Client Registration
(DCR) or a database of pre-registered clients.
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

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
        """Load statically configured clients from environment."""
        # Load from ALLOWED_MCP_CLIENTS environment variable
        allowed_clients = os.getenv("ALLOWED_MCP_CLIENTS", "").strip()

        if allowed_clients:
            # Parse comma-separated list
            for client_id in allowed_clients.split(","):
                client_id = client_id.strip()
                if client_id:
                    # Create basic client info
                    # In production, would load full metadata from database
                    self._clients[client_id] = MCPClientInfo(
                        client_id=client_id,
                        name=self._get_client_name(client_id),
                        redirect_uris=["http://localhost:*", "http://127.0.0.1:*"],
                        allowed_scopes=["openid", "profile", "email", "mcp-server:api"],
                        is_public=True,
                    )
                    logger.info(f"Registered static client: {client_id}")

        # Add well-known clients if not explicitly configured
        if not self._clients:
            self._add_well_known_clients()

    def _get_client_name(self, client_id: str) -> str:
        """Get human-readable name for client_id."""
        known_names = {
            "claude-desktop": "Claude Desktop",
            "continue-dev": "Continue IDE Extension",
            "zed-editor": "Zed Editor",
            "vscode-mcp": "VS Code MCP Extension",
            "test-mcp-client": "Test MCP Client",
        }
        return known_names.get(client_id, client_id.replace("-", " ").title())

    def _add_well_known_clients(self):
        """Add well-known MCP clients for testing and development."""
        well_known = [
            MCPClientInfo(
                client_id="claude-desktop",
                name="Claude Desktop",
                redirect_uris=["http://localhost:*", "http://127.0.0.1:*"],
                allowed_scopes=["openid", "profile", "email", "mcp-server:api"],
                is_public=True,
                metadata={"vendor": "Anthropic"},
            ),
            MCPClientInfo(
                client_id="test-mcp-client",
                name="Test MCP Client",
                redirect_uris=["http://localhost:*", "http://127.0.0.1:*"],
                allowed_scopes=["openid", "profile", "email", "mcp-server:api"],
                is_public=True,
                metadata={"purpose": "testing"},
            ),
        ]

        for client in well_known:
            self._clients[client.client_id] = client
            logger.info(f"Registered well-known client: {client.client_id}")

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
                logger.info(f"Unknown client {client_id}, would attempt DCR")
                return True, None
            else:
                return False, f"Unknown client_id: {client_id}"

        # Validate redirect_uri if provided
        if redirect_uri:
            if not self._validate_redirect_uri(client, redirect_uri):
                return False, f"Invalid redirect_uri for client {client_id}"

        # Validate scopes if provided
        if scopes:
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
        from urllib.parse import urlparse

        parsed = urlparse(redirect_uri)

        # Check against registered patterns
        for pattern in client.redirect_uris:
            if "*" in pattern:
                # Handle wildcard port (localhost:*)
                pattern_base = pattern.replace(":*", "")
                if redirect_uri.startswith(pattern_base + ":"):
                    # Validate it's localhost with a port
                    if parsed.hostname in ["localhost", "127.0.0.1"]:
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
            logger.warning(f"DCR disabled, cannot register {client_info.client_id}")
            return False

        if client_info.client_id in self._clients:
            logger.warning(f"Client {client_info.client_id} already registered")
            return False

        self._clients[client_info.client_id] = client_info
        logger.info(f"Dynamically registered client: {client_info.client_id}")

        # In production, would persist to database
        return True

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
        # Check if DCR is enabled
        allow_dcr = os.getenv("ENABLE_DCR", "false").lower() == "true"
        _registry = ClientRegistry(allow_dynamic_registration=allow_dcr)
    return _registry
