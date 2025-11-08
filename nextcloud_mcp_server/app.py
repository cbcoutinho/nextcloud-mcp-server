import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage

import anyio
import click
import httpx
import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from nextcloud_mcp_server.auth import (
    InsufficientScopeError,
    discover_all_scopes,
    get_access_token_scopes,
    has_required_scopes,
    is_jwt_token,
)
from nextcloud_mcp_server.auth.unified_verifier import UnifiedTokenVerifier
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import (
    LOGGING_CONFIG,
    get_document_processor_config,
    get_settings,
    setup_logging,
)
from nextcloud_mcp_server.context import get_client as get_nextcloud_client
from nextcloud_mcp_server.document_processors import get_registry
from nextcloud_mcp_server.server import (
    configure_calendar_tools,
    configure_contacts_tools,
    configure_cookbook_tools,
    configure_deck_tools,
    configure_notes_tools,
    configure_sharing_tools,
    configure_tables_tools,
    configure_webdav_tools,
)
from nextcloud_mcp_server.server.oauth_tools import register_oauth_tools
from nextcloud_mcp_server.vector import processor_task, scanner_task

logger = logging.getLogger(__name__)


def initialize_document_processors():
    """Initialize and register document processors based on configuration.

    This function reads the environment configuration and registers available
    processors (Unstructured, Tesseract, Custom HTTP) with the global registry.
    """
    config = get_document_processor_config()

    if not config["enabled"]:
        logger.info("Document processing disabled")
        return

    registry = get_registry()
    registered_count = 0

    # Register Unstructured processor
    if "unstructured" in config["processors"]:
        unst_config = config["processors"]["unstructured"]
        try:
            from nextcloud_mcp_server.document_processors.unstructured import (
                UnstructuredProcessor,
            )

            processor = UnstructuredProcessor(
                api_url=unst_config["api_url"],
                timeout=unst_config["timeout"],
                default_strategy=unst_config["strategy"],
                default_languages=unst_config["languages"],
                progress_interval=unst_config.get("progress_interval", 10),
            )
            registry.register(processor, priority=10)
            logger.info(f"Registered Unstructured processor: {unst_config['api_url']}")
            registered_count += 1
        except Exception as e:
            logger.warning(f"Failed to register Unstructured processor: {e}")

    # Register Tesseract processor
    if "tesseract" in config["processors"]:
        tess_config = config["processors"]["tesseract"]
        try:
            from nextcloud_mcp_server.document_processors.tesseract import (
                TesseractProcessor,
            )

            processor = TesseractProcessor(
                tesseract_cmd=tess_config.get("tesseract_cmd"),
                default_lang=tess_config["lang"],
            )
            registry.register(processor, priority=5)
            logger.info(f"Registered Tesseract processor: lang={tess_config['lang']}")
            registered_count += 1
        except Exception as e:
            logger.warning(f"Failed to register Tesseract processor: {e}")

    # Register custom processor
    if "custom" in config["processors"]:
        custom_config = config["processors"]["custom"]
        try:
            from nextcloud_mcp_server.document_processors.custom_http import (
                CustomHTTPProcessor,
            )

            processor = CustomHTTPProcessor(
                name=custom_config["name"],
                api_url=custom_config["api_url"],
                api_key=custom_config.get("api_key"),
                timeout=custom_config["timeout"],
                supported_types=custom_config["supported_types"],
            )
            registry.register(processor, priority=1)
            logger.info(
                f"Registered Custom processor '{custom_config['name']}': {custom_config['api_url']}"
            )
            registered_count += 1
        except Exception as e:
            logger.warning(f"Failed to register Custom processor: {e}")

    if registered_count > 0:
        logger.info(
            f"Document processing initialized with {registered_count} processor(s): "
            f"{', '.join(registry.list_processors())}"
        )
    else:
        logger.warning("Document processing enabled but no processors registered")


def validate_pkce_support(discovery: dict, discovery_url: str) -> None:
    """
    Validate that the OIDC provider properly advertises PKCE support.

    According to RFC 8414, if code_challenge_methods_supported is absent,
    it means the authorization server does not support PKCE.

    MCP clients require PKCE with S256 and will refuse to connect if this
    field is missing or doesn't include S256.
    """

    code_challenge_methods = discovery.get("code_challenge_methods_supported")

    if code_challenge_methods is None:
        click.echo("=" * 80, err=True)
        click.echo(
            "ERROR: OIDC CONFIGURATION ERROR - Missing PKCE Support Advertisement",
            err=True,
        )
        click.echo("=" * 80, err=True)
        click.echo(f"Discovery URL: {discovery_url}", err=True)
        click.echo("", err=True)
        click.echo(
            "The OIDC discovery document is missing 'code_challenge_methods_supported'.",
            err=True,
        )
        click.echo(
            "According to RFC 8414, this means the server does NOT support PKCE.",
            err=True,
        )
        click.echo("", err=True)
        click.echo("⚠️  MCP clients (like Claude Code) WILL REJECT this provider!")
        click.echo("", err=True)
        click.echo("How to fix:", err=True)
        click.echo(
            "  1. Ensure PKCE is enabled in Nextcloud OIDC app settings", err=True
        )
        click.echo(
            "  2. Update the OIDC app to advertise PKCE support in discovery", err=True
        )
        click.echo("  3. See: RFC 8414 Section 2 (Authorization Server Metadata)")
        click.echo("=" * 80, err=True)
        click.echo("", err=True)
        return

    if "S256" not in code_challenge_methods:
        click.echo("=" * 80, err=True)
        click.echo(
            "WARNING: OIDC CONFIGURATION WARNING - S256 Challenge Method Not Advertised",
            err=True,
        )
        click.echo("=" * 80, err=True)
        click.echo(f"Discovery URL: {discovery_url}", err=True)
        click.echo(f"Advertised methods: {code_challenge_methods}", err=True)
        click.echo("", err=True)
        click.echo("MCP specification requires S256 code challenge method.", err=True)
        click.echo("Some clients may reject this provider.", err=True)
        click.echo("=" * 80, err=True)
        click.echo("", err=True)
        return

    click.echo(f"✓ PKCE support validated: {code_challenge_methods}")


@dataclass
class AppContext:
    """Application context for BasicAuth mode."""

    client: NextcloudClient
    document_queue: Optional[asyncio.Queue] = None
    shutdown_event: Optional[anyio.Event] = None
    scanner_wake_event: Optional[anyio.Event] = None


@dataclass
class OAuthAppContext:
    """Application context for OAuth mode."""

    nextcloud_host: str
    token_verifier: object  # UnifiedTokenVerifier (ADR-005 compliant)
    refresh_token_storage: Optional["RefreshTokenStorage"] = None
    oauth_client: Optional[object] = None  # NextcloudOAuthClient or KeycloakOAuthClient
    oauth_provider: str = "nextcloud"  # "nextcloud" or "keycloak"
    server_client_id: Optional[str] = (
        None  # MCP server's OAuth client ID (static or DCR)
    )


def is_oauth_mode() -> bool:
    """
    Determine if OAuth mode should be used.

    OAuth mode is enabled when:
    - NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD are NOT set
    - Or explicitly enabled via configuration

    Returns:
        True if OAuth mode, False if BasicAuth mode
    """
    username = os.getenv("NEXTCLOUD_USERNAME")
    password = os.getenv("NEXTCLOUD_PASSWORD")

    # If both username and password are set, use BasicAuth
    if username and password:
        logger.info(
            "BasicAuth mode detected (NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD set)"
        )
        return False

    logger.info("OAuth mode detected (NEXTCLOUD_USERNAME/PASSWORD not set)")
    return True


async def load_oauth_client_credentials(
    nextcloud_host: str, registration_endpoint: str | None
) -> tuple[str, str]:
    """
    Load OAuth client credentials from environment, storage file, or dynamic registration.

    This consolidates the client loading logic that was duplicated across multiple functions.

    Args:
        nextcloud_host: Nextcloud instance URL
        registration_endpoint: Dynamic registration endpoint URL (or None if not available)

    Returns:
        Tuple of (client_id, client_secret)

    Raises:
        ValueError: If credentials cannot be obtained
    """
    # Try environment variables first
    client_id = os.getenv("NEXTCLOUD_OIDC_CLIENT_ID")
    client_secret = os.getenv("NEXTCLOUD_OIDC_CLIENT_SECRET")

    if client_id and client_secret:
        logger.info("Using pre-configured OAuth client credentials from environment")
        return (client_id, client_secret)

    # Try loading from SQLite storage
    try:
        from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage

        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

        client_data = await storage.get_oauth_client()
        if client_data:
            logger.info(
                f"Loaded OAuth client from SQLite: {client_data['client_id'][:16]}..."
            )
            return (client_data["client_id"], client_data["client_secret"])
    except ValueError:
        # TOKEN_ENCRYPTION_KEY not set, skip SQLite storage check
        logger.debug("SQLite storage not available (TOKEN_ENCRYPTION_KEY not set)")

    # Try dynamic registration if available
    if registration_endpoint:
        logger.info("Dynamic client registration available")
        mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")
        redirect_uris = [
            f"{mcp_server_url}/oauth/callback",  # Unified callback (flow determined by query param)
        ]

        # MCP server DCR: Register with ALL supported scopes
        # When we register as a resource server (with resource_url), the allowed_scopes
        # represent what scopes are AVAILABLE for this resource, not what the server needs.
        # External clients will request tokens with resource=http://localhost:8001/mcp
        # and the authorization server will limit them to these allowed scopes.
        #
        # The PRM endpoint advertises the same scopes dynamically via @require_scopes decorators.
        dcr_scopes = "openid profile email notes:read notes:write calendar:read calendar:write todo:read todo:write contacts:read contacts:write cookbook:read cookbook:write deck:read deck:write tables:read tables:write files:read files:write sharing:read sharing:write"

        # Add offline_access scope if refresh tokens are enabled
        enable_offline_access = os.getenv("ENABLE_OFFLINE_ACCESS", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        if enable_offline_access:
            dcr_scopes = f"{dcr_scopes} offline_access"
            logger.info("✓ offline_access scope enabled for refresh tokens")

        logger.info(f"MCP server DCR scopes (resource server): {dcr_scopes}")

        # Get token type from environment (Bearer or jwt)
        # Note: Must be lowercase "jwt" to match OIDC app's check
        token_type = os.getenv("NEXTCLOUD_OIDC_TOKEN_TYPE", "Bearer").lower()
        # Special case: "bearer" should remain capitalized for compatibility
        if token_type != "jwt":
            token_type = "Bearer"
        logger.info(f"Requesting token type: {token_type}")

        # Ensure OAuth client in SQLite storage
        from nextcloud_mcp_server.auth.client_registration import ensure_oauth_client
        from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage

        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

        # RFC 9728: resource_url must be a URL for the protected resource
        # This URL is used by token introspection to match tokens to this client
        resource_url = f"{mcp_server_url}/mcp"

        client_info = await ensure_oauth_client(
            nextcloud_url=nextcloud_host,
            registration_endpoint=registration_endpoint,
            storage=storage,
            client_name=f"Nextcloud MCP Server ({token_type})",
            redirect_uris=redirect_uris,
            scopes=dcr_scopes,  # Use DCR-specific scopes (basic OIDC only)
            token_type=token_type,
            resource_url=resource_url,  # RFC 9728 Protected Resource URL
        )

        logger.info(f"OAuth client ready: {client_info.client_id[:16]}...")
        return (client_info.client_id, client_info.client_secret)

    # No credentials available
    raise ValueError(
        "OAuth mode requires either:\n"
        "1. NEXTCLOUD_OIDC_CLIENT_ID and NEXTCLOUD_OIDC_CLIENT_SECRET environment variables, OR\n"
        "2. Pre-existing client credentials in SQLite storage (TOKEN_STORAGE_DB), OR\n"
        "3. Dynamic client registration enabled on Nextcloud OIDC app\n\n"
        "Note: TOKEN_ENCRYPTION_KEY is required for SQLite storage"
    )


@asynccontextmanager
async def app_lifespan_basic(server: FastMCP) -> AsyncIterator[AppContext]:
    """
    Manage application lifecycle for BasicAuth mode.

    Creates a single Nextcloud client with basic authentication
    that is shared across all requests.

    If vector sync is enabled (VECTOR_SYNC_ENABLED=true), also starts
    background tasks for automatic document indexing (ADR-007).
    """
    logger.info("Starting MCP server in BasicAuth mode")
    logger.info("Creating Nextcloud client with BasicAuth")

    client = NextcloudClient.from_env()
    logger.info("Client initialization complete")

    # Initialize document processors
    initialize_document_processors()

    settings = get_settings()

    # Check if vector sync is enabled
    if settings.vector_sync_enabled:
        logger.info("Vector sync enabled - starting background tasks")

        # Get username from environment for BasicAuth mode
        username = os.getenv("NEXTCLOUD_USERNAME")
        if not username:
            raise ValueError(
                "NEXTCLOUD_USERNAME is required for vector sync in BasicAuth mode"
            )

        # Initialize shared state
        document_queue = asyncio.Queue(maxsize=settings.vector_sync_queue_max_size)
        shutdown_event = anyio.Event()
        scanner_wake_event = anyio.Event()

        # Start background tasks using anyio TaskGroup
        async with anyio.create_task_group() as tg:
            # Start scanner task
            tg.start_soon(
                scanner_task,
                document_queue,
                shutdown_event,
                scanner_wake_event,
                client,
                username,
            )

            # Start processor pool
            for i in range(settings.vector_sync_processor_workers):
                tg.start_soon(
                    processor_task,
                    i,
                    document_queue,
                    shutdown_event,
                    client,
                    username,
                )

            logger.info(
                f"Background sync tasks started: 1 scanner + {settings.vector_sync_processor_workers} processors"
            )

            # Yield with background tasks running
            try:
                yield AppContext(
                    client=client,
                    document_queue=document_queue,
                    shutdown_event=shutdown_event,
                    scanner_wake_event=scanner_wake_event,
                )
            finally:
                # Shutdown signal
                logger.info("Shutting down background sync tasks")
                shutdown_event.set()

                # TaskGroup automatically cancels all tasks on exit
                logger.info("Background sync tasks stopped")
                await client.close()
    else:
        # No vector sync - simple lifecycle
        try:
            yield AppContext(client=client)
        finally:
            logger.info("Shutting down BasicAuth mode")
            await client.close()


async def setup_oauth_config():
    """
    Setup OAuth configuration by performing OIDC discovery and client registration.

    Auto-detects OAuth provider mode:
    - Integrated mode: OIDC_DISCOVERY_URL points to NEXTCLOUD_HOST (or not set)
      → Nextcloud OIDC app provides both OAuth and API access
    - External IdP mode: OIDC_DISCOVERY_URL points to external provider
      → External IdP for OAuth, Nextcloud user_oidc validates tokens and provides API access

    Uses generic OIDC environment variables:
    - OIDC_DISCOVERY_URL: OIDC discovery endpoint (optional, defaults to NEXTCLOUD_HOST)
    - OIDC_CLIENT_ID / OIDC_CLIENT_SECRET: Static credentials (optional, uses DCR if not provided)
    - NEXTCLOUD_OIDC_SCOPES: Requested OAuth scopes

    This is done synchronously before FastMCP initialization because FastMCP
    requires token_verifier at construction time.

    Returns:
        Tuple of (nextcloud_host, token_verifier, auth_settings, refresh_token_storage, oauth_client, oauth_provider, client_id, client_secret)
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        raise ValueError(
            "NEXTCLOUD_HOST environment variable is required for OAuth mode"
        )

    nextcloud_host = nextcloud_host.rstrip("/")

    # Get OIDC discovery URL (defaults to Nextcloud integrated mode)
    discovery_url = os.getenv(
        "OIDC_DISCOVERY_URL", f"{nextcloud_host}/.well-known/openid-configuration"
    )
    logger.info(f"Performing OIDC discovery: {discovery_url}")

    # Perform OIDC discovery
    async with httpx.AsyncClient() as client:
        response = await client.get(discovery_url)
        response.raise_for_status()
        discovery = response.json()

    logger.info("✓ OIDC discovery successful")

    # Validate PKCE support
    validate_pkce_support(discovery, discovery_url)

    # Extract OIDC endpoints
    issuer = discovery["issuer"]
    userinfo_uri = discovery["userinfo_endpoint"]
    jwks_uri = discovery.get("jwks_uri")
    introspection_uri = discovery.get("introspection_endpoint")
    registration_endpoint = discovery.get("registration_endpoint")

    # Allow overriding JWKS URI (useful when running in Docker with frontendUrl)
    # Example: frontendUrl=http://localhost:8888 but MCP server needs http://keycloak:8080
    jwks_uri_override = os.getenv("OIDC_JWKS_URI")
    if jwks_uri_override:
        logger.info(f"OIDC_JWKS_URI override: {jwks_uri} → {jwks_uri_override}")
        jwks_uri = jwks_uri_override

    logger.info("OIDC endpoints discovered:")
    logger.info(f"  Issuer: {issuer}")
    logger.info(f"  Userinfo: {userinfo_uri}")
    if jwks_uri:
        logger.info(f"  JWKS: {jwks_uri}")
    if introspection_uri:
        logger.info(f"  Introspection: {introspection_uri}")

    # Auto-detect provider mode based on issuer
    # External IdP mode: issuer doesn't match Nextcloud host
    # Normalize URLs for comparison (handle port differences like :80 for HTTP)
    from urllib.parse import urlparse

    def normalize_url(url: str) -> str:
        """Normalize URL by removing default ports (80 for HTTP, 443 for HTTPS)."""
        parsed = urlparse(url)
        # Remove default ports
        if (parsed.scheme == "http" and parsed.port == 80) or (
            parsed.scheme == "https" and parsed.port == 443
        ):
            # Remove explicit default port
            hostname = parsed.hostname or parsed.netloc.split(":")[0]
            return f"{parsed.scheme}://{hostname}"
        return f"{parsed.scheme}://{parsed.netloc}"

    issuer_normalized = normalize_url(issuer)
    nextcloud_normalized = normalize_url(nextcloud_host)

    is_external_idp = not issuer_normalized.startswith(nextcloud_normalized)

    if is_external_idp:
        oauth_provider = "external"  # Could be Keycloak, Auth0, Okta, etc.
        logger.info(
            f"✓ Detected external IdP mode (issuer: {issuer} != Nextcloud: {nextcloud_host})"
        )
        logger.info("  Tokens will be validated via Nextcloud user_oidc app")
    else:
        oauth_provider = "nextcloud"
        logger.info("✓ Detected integrated mode (Nextcloud OIDC app)")

    # Check if offline access (refresh tokens) is enabled
    enable_offline_access = os.getenv("ENABLE_OFFLINE_ACCESS", "false").lower() in (
        "true",
        "1",
        "yes",
    )

    # Initialize refresh token storage if enabled
    refresh_token_storage = None
    if enable_offline_access:
        try:
            from nextcloud_mcp_server.auth.refresh_token_storage import (
                RefreshTokenStorage,
            )

            # Validate encryption key before initializing
            encryption_key = os.getenv("TOKEN_ENCRYPTION_KEY")
            if not encryption_key:
                logger.warning(
                    "ENABLE_OFFLINE_ACCESS=true but TOKEN_ENCRYPTION_KEY not set. "
                    "Refresh tokens will NOT be stored. Generate a key with:\n"
                    '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
                )
            else:
                refresh_token_storage = RefreshTokenStorage.from_env()
                await refresh_token_storage.initialize()
                logger.info(
                    "✓ Refresh token storage initialized (offline_access enabled)"
                )
        except Exception as e:
            logger.error(f"Failed to initialize refresh token storage: {e}")
            logger.warning(
                "Continuing without refresh token storage - users will need to re-authenticate after token expiration"
            )

    # Load client credentials (static or dynamic registration)
    client_id = os.getenv("OIDC_CLIENT_ID")
    client_secret = os.getenv("OIDC_CLIENT_SECRET")

    if client_id and client_secret:
        logger.info(f"Using static OIDC client credentials: {client_id}")
    elif registration_endpoint:
        logger.info("OIDC_CLIENT_ID not set, attempting Dynamic Client Registration")
        client_id, client_secret = await load_oauth_client_credentials(
            nextcloud_host=nextcloud_host, registration_endpoint=registration_endpoint
        )
    else:
        raise ValueError(
            "OIDC_CLIENT_ID and OIDC_CLIENT_SECRET environment variables are required "
            "when the OIDC provider does not support Dynamic Client Registration. "
            f"Discovery URL: {discovery_url}"
        )

    # Handle public issuer override (for clients accessing via different URL)
    # When clients access Nextcloud via a public URL (e.g., http://127.0.0.1:8080),
    # but the MCP server accesses via internal URL (e.g., http://app:80),
    # we need to use the public URL for JWT validation and client configuration
    public_issuer = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL")
    if public_issuer:
        public_issuer = public_issuer.rstrip("/")
        logger.info(
            f"Using public issuer URL override for JWT validation: {public_issuer}"
        )
        client_issuer = public_issuer
    else:
        client_issuer = issuer

    # ADR-005: Unified Token Verifier with proper audience validation
    # Get MCP server URL for audience validation
    mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")
    nextcloud_resource_uri = os.getenv("NEXTCLOUD_RESOURCE_URI", nextcloud_host)

    # Warn if resource URIs are not configured (required for ADR-005 compliance)
    if not os.getenv("NEXTCLOUD_MCP_SERVER_URL"):
        logger.warning(
            f"NEXTCLOUD_MCP_SERVER_URL not set, defaulting to: {mcp_server_url}. "
            "This should be set explicitly for proper audience validation."
        )
    if not os.getenv("NEXTCLOUD_RESOURCE_URI"):
        logger.warning(
            f"NEXTCLOUD_RESOURCE_URI not set, defaulting to: {nextcloud_resource_uri}. "
            "This should be set explicitly for proper audience validation."
        )

    # Create settings for UnifiedTokenVerifier
    from nextcloud_mcp_server.config import get_settings

    settings = get_settings()
    # Override with discovered values if not set in environment
    if not settings.oidc_client_id:
        settings.oidc_client_id = client_id
    if not settings.oidc_client_secret:
        settings.oidc_client_secret = client_secret
    if not settings.jwks_uri:
        settings.jwks_uri = jwks_uri
    if not settings.introspection_uri:
        settings.introspection_uri = introspection_uri
    if not settings.userinfo_uri:
        settings.userinfo_uri = userinfo_uri
    if not settings.oidc_issuer:
        # Use client_issuer which handles public URL override
        settings.oidc_issuer = client_issuer
    if not settings.nextcloud_mcp_server_url:
        settings.nextcloud_mcp_server_url = mcp_server_url
    if not settings.nextcloud_resource_uri:
        settings.nextcloud_resource_uri = nextcloud_resource_uri

    # Create Unified Token Verifier (ADR-005 compliant)
    token_verifier = UnifiedTokenVerifier(settings)

    # Log the mode
    enable_token_exchange = (
        os.getenv("ENABLE_TOKEN_EXCHANGE", "false").lower() == "true"
    )
    if enable_token_exchange:
        logger.info(
            "✓ Token Exchange mode enabled (ADR-005) - exchanging MCP tokens for Nextcloud tokens via RFC 8693"
        )
        logger.info(f"  MCP audience: {client_id} or {mcp_server_url}")
        logger.info(f"  Nextcloud audience: {nextcloud_resource_uri}")
    else:
        logger.info(
            "✓ Multi-audience mode enabled (ADR-005) - tokens must contain both MCP and Nextcloud audiences"
        )
        logger.info(f"  Required MCP audience: {client_id} or {mcp_server_url}")
        logger.info(f"  Required Nextcloud audience: {nextcloud_resource_uri}")

    if introspection_uri:
        logger.info("✓ Opaque token introspection enabled (RFC 7662)")
    if jwks_uri:
        logger.info("✓ JWT signature verification enabled (JWKS)")

    # Progressive Consent mode (for offline access / background jobs)
    encryption_key = os.getenv("TOKEN_ENCRYPTION_KEY")
    if enable_offline_access and encryption_key and refresh_token_storage:
        logger.info("✓ Progressive Consent mode enabled - offline access available")

        # Note: Token Broker service would be initialized here for background job support
        # Currently not used in ADR-005 implementation as it's specific to offline access patterns
        # that are separate from the real-time token exchange flow
        logger.debug("Token broker available for future offline access features")

    # Create OAuth client for server-initiated flows (e.g., token exchange, background workers)
    oauth_client = None
    if enable_offline_access and refresh_token_storage and is_external_idp:
        # For external IdP mode, create generic OIDC client for token operations
        from nextcloud_mcp_server.auth.keycloak_oauth import KeycloakOAuthClient

        mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")
        # Note: This redirect_uri is for OAuth client initialization, not used for actual redirects
        # since this client is used for backend token operations (exchange, refresh)
        redirect_uri = f"{mcp_server_url}/oauth/callback"

        # Extract base URL and realm from discovery URL
        # Format: http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration
        # → base_url: http://keycloak:8080, realm: nextcloud-mcp
        if "/realms/" in discovery_url:
            base_url = discovery_url.split("/realms/")[0]
            realm = discovery_url.split("/realms/")[1].split("/")[0]
        else:
            # Fallback: use issuer to extract base URL
            base_url = (
                issuer.rsplit("/realms/", 1)[0] if "/realms/" in issuer else issuer
            )
            realm = issuer.split("/realms/")[1] if "/realms/" in issuer else ""

        oauth_client = KeycloakOAuthClient(
            keycloak_url=base_url,
            realm=realm,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
        await oauth_client.discover()
        logger.info(
            "✓ OIDC client initialized for token operations (token exchange, refresh)"
        )
    elif enable_offline_access and refresh_token_storage:
        # For integrated mode, OAuth client could be added later
        # For now, token refresh can use httpx directly with discovered endpoints
        logger.info(
            "OAuth client for token refresh not yet implemented for integrated mode"
        )

    # Create auth settings
    mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")

    # Note: We don't set required_scopes here anymore.
    # Scopes are now advertised via PRM endpoint and enforced per-tool.
    # This allows dynamic tool filtering based on user's actual token scopes.
    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(
            client_issuer
        ),  # Use client issuer (may be public override)
        resource_server_url=AnyHttpUrl(mcp_server_url),
    )

    logger.info("OAuth configuration complete")

    return (
        nextcloud_host,
        token_verifier,
        auth_settings,
        refresh_token_storage,
        oauth_client,
        oauth_provider,
        client_id,
        client_secret,
    )


def get_app(transport: str = "sse", enabled_apps: list[str] | None = None):
    setup_logging()

    # Determine authentication mode
    oauth_enabled = is_oauth_mode()

    if oauth_enabled:
        logger.info("Configuring MCP server for OAuth mode")
        # Asynchronously get the OAuth configuration
        import anyio

        (
            nextcloud_host,
            token_verifier,
            auth_settings,
            refresh_token_storage,
            oauth_client,
            oauth_provider,
            client_id,
            client_secret,
        ) = anyio.run(setup_oauth_config)

        # Create lifespan function with captured OAuth context (closure)
        @asynccontextmanager
        async def oauth_lifespan(server: FastMCP) -> AsyncIterator[OAuthAppContext]:
            """
            Lifespan context for OAuth mode - captures OAuth configuration from outer scope.
            """
            logger.info("Starting MCP server in OAuth mode")
            logger.info(f"Using OAuth provider: {oauth_provider}")
            if refresh_token_storage:
                logger.info("Refresh token storage is available")
            if oauth_client:
                logger.info("OAuth client is available for token refresh")

            # Initialize document processors
            initialize_document_processors()

            try:
                yield OAuthAppContext(
                    nextcloud_host=nextcloud_host,
                    token_verifier=token_verifier,
                    refresh_token_storage=refresh_token_storage,
                    oauth_client=oauth_client,
                    oauth_provider=oauth_provider,
                    server_client_id=client_id,
                )
            finally:
                logger.info("Shutting down MCP server")
                # RefreshTokenStorage uses context managers, no close() needed
                # OAuth client cleanup (if it has a close method)
                if oauth_client and hasattr(oauth_client, "close"):
                    try:
                        await oauth_client.close()
                    except Exception as e:
                        logger.warning(f"Error closing OAuth client: {e}")
                logger.info("MCP server shutdown complete")

        mcp = FastMCP(
            "Nextcloud MCP",
            lifespan=oauth_lifespan,
            token_verifier=token_verifier,
            auth=auth_settings,
        )
    else:
        logger.info("Configuring MCP server for BasicAuth mode")
        mcp = FastMCP("Nextcloud MCP", lifespan=app_lifespan_basic)

    @mcp.resource("nc://capabilities")
    async def nc_get_capabilities():
        """Get the Nextcloud Host capabilities"""
        ctx: Context = mcp.get_context()
        client = await get_nextcloud_client(ctx)
        return await client.capabilities()

    # Define available apps and their configuration functions
    available_apps = {
        "notes": configure_notes_tools,
        "tables": configure_tables_tools,
        "webdav": configure_webdav_tools,
        "sharing": configure_sharing_tools,
        "calendar": configure_calendar_tools,
        "contacts": configure_contacts_tools,
        "cookbook": configure_cookbook_tools,
        "deck": configure_deck_tools,
    }

    # If no specific apps are specified, enable all
    if enabled_apps is None:
        enabled_apps = list(available_apps.keys())

    # Configure only the enabled apps
    for app_name in enabled_apps:
        if app_name in available_apps:
            logger.info(f"Configuring {app_name} tools")
            available_apps[app_name](mcp)
        else:
            logger.warning(
                f"Unknown app: {app_name}. Available apps: {list(available_apps.keys())}"
            )

    # Register OAuth provisioning tools (only when offline access is enabled)
    # With token exchange enabled (external IdP), provisioning is not needed for MCP operations
    enable_token_exchange = (
        os.getenv("ENABLE_TOKEN_EXCHANGE", "false").lower() == "true"
    )
    enable_offline_access_for_tools = os.getenv(
        "ENABLE_OFFLINE_ACCESS", "false"
    ).lower() in (
        "true",
        "1",
        "yes",
    )
    if oauth_enabled and enable_offline_access_for_tools and not enable_token_exchange:
        logger.info("Registering OAuth provisioning tools for offline access")
        register_oauth_tools(mcp)
    elif oauth_enabled and enable_token_exchange:
        logger.info("Skipping provisioning tools registration (token exchange enabled)")
    elif oauth_enabled and not enable_offline_access_for_tools:
        logger.info(
            "Skipping provisioning tools registration (offline access not enabled)"
        )

    # Override list_tools to filter based on user's token scopes (OAuth mode only)
    if oauth_enabled:
        original_list_tools = mcp._tool_manager.list_tools

        def list_tools_filtered():
            """List tools filtered by user's token scopes (JWT and Bearer tokens)."""
            # Get user's scopes from token using MCP SDK's contextvar
            # This works for all request types including list_tools
            user_scopes = get_access_token_scopes()
            is_jwt = is_jwt_token()
            logger.info(
                f"🔍 list_tools called - Token type: {'JWT' if is_jwt else 'opaque/none'}, "
                f"User scopes: {user_scopes}"
            )

            # Get all tools
            all_tools = original_list_tools()

            # Filter tools based on user's token scopes (both JWT and opaque tokens)
            # JWT tokens have scopes embedded in payload
            # Opaque tokens get scopes via introspection endpoint
            # Claude Code now properly respects PRM endpoint for scope discovery
            if user_scopes:
                allowed_tools = [
                    tool
                    for tool in all_tools
                    if has_required_scopes(tool.fn, user_scopes)
                ]
                token_type = "JWT" if is_jwt else "Bearer"
                logger.info(
                    f"✂️ {token_type} scope filtering: {len(allowed_tools)}/{len(all_tools)} tools "
                    f"available for scopes: {user_scopes}"
                )
            else:
                # BasicAuth mode or no token - show all tools
                allowed_tools = all_tools
                logger.info(
                    f"📋 Showing all {len(all_tools)} tools (no token/BasicAuth)"
                )

            # Return the Tool objects directly (they're already in the correct format)
            return allowed_tools

        # Replace the tool manager's list_tools method
        mcp._tool_manager.list_tools = list_tools_filtered  # type: ignore[method-assign]
        logger.info(
            "Dynamic tool filtering enabled for OAuth mode (JWT and Bearer tokens)"
        )

    if transport == "sse":
        mcp_app = mcp.sse_app()
        starlette_lifespan = None
    elif transport in ("http", "streamable-http"):
        mcp_app = mcp.streamable_http_app()

        @asynccontextmanager
        async def starlette_lifespan(app: Starlette):
            # Set OAuth context for OAuth login routes (ADR-004)
            if oauth_enabled:
                # Prepare OAuth config from setup_oauth_config closure variables
                mcp_server_url = os.getenv(
                    "NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000"
                )
                nextcloud_resource_uri = os.getenv(
                    "NEXTCLOUD_RESOURCE_URI", nextcloud_host
                )
                discovery_url = os.getenv(
                    "OIDC_DISCOVERY_URL",
                    f"{nextcloud_host}/.well-known/openid-configuration",
                )
                scopes = os.getenv("NEXTCLOUD_OIDC_SCOPES", "")

                oauth_context_dict = {
                    "storage": refresh_token_storage,
                    "oauth_client": oauth_client,
                    "token_verifier": token_verifier,  # For querying IdP userinfo endpoint
                    "config": {
                        "mcp_server_url": mcp_server_url,
                        "discovery_url": discovery_url,
                        "client_id": client_id,  # From setup_oauth_config (DCR or static)
                        "client_secret": client_secret,  # From setup_oauth_config (DCR or static)
                        "scopes": scopes,
                        "nextcloud_host": nextcloud_host,
                        "nextcloud_resource_uri": nextcloud_resource_uri,
                        "oauth_provider": oauth_provider,
                    },
                }
                app.state.oauth_context = oauth_context_dict

                # Also set oauth_context on browser_app for session authentication
                # browser_app is in the same function scope (defined later in create_app)
                # We need to find it in the mounted routes
                for route in app.routes:
                    if isinstance(route, Mount) and route.path == "/user":
                        route.app.state.oauth_context = oauth_context_dict
                        logger.info(
                            "OAuth context shared with browser_app for session auth"
                        )
                        break

                logger.info(
                    f"OAuth context initialized for login routes (client_id={client_id[:16]}...)"
                )

            async with AsyncExitStack() as stack:
                await stack.enter_async_context(mcp.session_manager.run())
                yield

    # Health check endpoints for Kubernetes probes
    def health_live(request):
        """Liveness probe endpoint.

        Returns 200 OK if the application process is running.
        This is a simple check that doesn't verify external dependencies.
        """
        return JSONResponse(
            {
                "status": "alive",
                "mode": "oauth" if oauth_enabled else "basic",
            }
        )

    async def health_ready(request):
        """Readiness probe endpoint.

        Returns 200 OK if the application is ready to serve traffic.
        Checks that required configuration is present and Qdrant if vector sync enabled.
        """
        checks = {}
        is_ready = True

        # Check Nextcloud host configuration
        nextcloud_host = os.getenv("NEXTCLOUD_HOST")
        if nextcloud_host:
            checks["nextcloud_configured"] = "ok"
        else:
            checks["nextcloud_configured"] = "error: NEXTCLOUD_HOST not set"
            is_ready = False

        # Check authentication configuration
        if oauth_enabled:
            # OAuth mode - just verify we got this far (token_verifier initialized in lifespan)
            checks["auth_mode"] = "oauth"
            checks["auth_configured"] = "ok"
        else:
            # BasicAuth mode - verify credentials are set
            username = os.getenv("NEXTCLOUD_USERNAME")
            password = os.getenv("NEXTCLOUD_PASSWORD")
            if username and password:
                checks["auth_mode"] = "basic"
                checks["auth_configured"] = "ok"
            else:
                checks["auth_mode"] = "basic"
                checks["auth_configured"] = "error: credentials not set"
                is_ready = False

        # Check Qdrant status if vector sync is enabled
        vector_sync_enabled = (
            os.getenv("VECTOR_SYNC_ENABLED", "false").lower() == "true"
        )
        if vector_sync_enabled:
            try:
                qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"{qdrant_url}/readyz")
                    if response.status_code == 200:
                        checks["qdrant"] = "ok"
                    else:
                        checks["qdrant"] = f"error: status {response.status_code}"
                        is_ready = False
            except Exception as e:
                checks["qdrant"] = f"error: {str(e)}"
                is_ready = False

        status_code = 200 if is_ready else 503
        return JSONResponse(
            {
                "status": "ready" if is_ready else "not_ready",
                "checks": checks,
            },
            status_code=status_code,
        )

    # Add Protected Resource Metadata (PRM) endpoint for OAuth mode
    routes = []

    # Add health check routes (available in both OAuth and BasicAuth modes)
    routes.append(Route("/health/live", health_live, methods=["GET"]))
    routes.append(Route("/health/ready", health_ready, methods=["GET"]))
    logger.info("Health check endpoints enabled: /health/live, /health/ready")

    if oauth_enabled:
        # Import OAuth routes (ADR-004 Progressive Consent)
        from nextcloud_mcp_server.auth.oauth_routes import oauth_authorize

        def oauth_protected_resource_metadata(request):
            """RFC 9728 Protected Resource Metadata endpoint.

            Dynamically discovers supported scopes from registered MCP tools.
            This ensures the advertised scopes always match the actual tool requirements.

            The 'resource' field is set to the MCP server's public URL (RFC 9728 requires a URL).
            This is used as the audience in access tokens via the resource parameter (RFC 8707).
            The introspection controller matches this URL to the MCP server's client via resource_url field.
            """
            # Use PUBLIC_ISSUER_URL for authorization server since external clients
            # (like Claude) need the publicly accessible URL, not internal Docker URLs
            public_issuer_url = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL")
            if not public_issuer_url:
                # Fallback to NEXTCLOUD_HOST if PUBLIC_ISSUER_URL not set
                public_issuer_url = os.getenv("NEXTCLOUD_HOST", "")

            # RFC 9728 requires resource to be a URL (not a client ID)
            # Use the MCP server's public URL
            mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL")
            if not mcp_server_url:
                # Fallback to constructing from host and port
                mcp_server_url = f"http://localhost:{os.getenv('PORT', '8000')}"

            # Dynamically discover all scopes from registered tools
            # This provides a single source of truth based on @require_scopes decorators
            supported_scopes = discover_all_scopes(mcp)

            return JSONResponse(
                {
                    "resource": f"{mcp_server_url}/mcp",  # RFC 9728: must be a URL
                    "scopes_supported": supported_scopes,
                    "authorization_servers": [public_issuer_url],
                    "bearer_methods_supported": ["header"],
                    "resource_signing_alg_values_supported": ["RS256"],
                }
            )

        # Register PRM endpoint at both path-based and root locations per RFC 9728
        # Path-based discovery: /.well-known/oauth-protected-resource{path}
        routes.append(
            Route(
                "/.well-known/oauth-protected-resource/mcp",
                oauth_protected_resource_metadata,
                methods=["GET"],
            )
        )
        # Root discovery (fallback): /.well-known/oauth-protected-resource
        routes.append(
            Route(
                "/.well-known/oauth-protected-resource",
                oauth_protected_resource_metadata,
                methods=["GET"],
            )
        )
        logger.info(
            "Protected Resource Metadata (PRM) endpoints enabled (path-based + root)"
        )

        # Add OAuth login routes (ADR-004 Progressive Consent Flow 1)
        routes.append(Route("/oauth/authorize", oauth_authorize, methods=["GET"]))
        logger.info("OAuth login routes enabled: /oauth/authorize (Flow 1)")

        # Add unified OAuth callback endpoint supporting both flows
        from nextcloud_mcp_server.auth.oauth_routes import (
            oauth_authorize_nextcloud,
            oauth_callback,
            oauth_callback_nextcloud,
        )

        routes.append(Route("/oauth/callback", oauth_callback, methods=["GET"]))
        logger.info(
            "OAuth unified callback enabled: /oauth/callback?flow={browser|provisioning}"
        )

        # Add OAuth resource provisioning routes (ADR-004 Progressive Consent Flow 2)
        routes.append(
            Route(
                "/oauth/authorize-nextcloud",
                oauth_authorize_nextcloud,
                methods=["GET"],
            )
        )
        # Keep old callback endpoint as backwards-compatible alias
        routes.append(
            Route(
                "/oauth/callback-nextcloud",
                oauth_callback_nextcloud,
                methods=["GET"],
            )
        )
        logger.info(
            "OAuth resource provisioning routes enabled: /oauth/authorize-nextcloud, /oauth/callback-nextcloud (Flow 2, legacy)"
        )

    # Add browser OAuth login routes (OAuth mode only)
    if oauth_enabled:
        from nextcloud_mcp_server.auth.browser_oauth_routes import (
            oauth_login,
            oauth_login_callback,
            oauth_logout,
        )

        routes.append(
            Route("/oauth/login", oauth_login, methods=["GET"], name="oauth_login")
        )
        # Keep old callback endpoint as backwards-compatible alias
        routes.append(
            Route(
                "/oauth/login-callback",
                oauth_login_callback,
                methods=["GET"],
                name="oauth_login_callback",
            )
        )
        routes.append(
            Route("/oauth/logout", oauth_logout, methods=["GET"], name="oauth_logout")
        )
        logger.info(
            "Browser OAuth routes enabled: /oauth/login, /oauth/login-callback (legacy), /oauth/logout"
        )

    # Add user info routes (available in both BasicAuth and OAuth modes)
    # These require session authentication, so we wrap them in a separate app
    from nextcloud_mcp_server.auth.session_backend import SessionAuthBackend
    from nextcloud_mcp_server.auth.userinfo_routes import (
        revoke_session,
        user_info_html,
        user_info_json,
    )

    # Create a separate Starlette app for browser routes that need session auth
    # This prevents SessionAuthBackend from interfering with FastMCP's OAuth
    browser_routes = [
        Route("/", user_info_json, methods=["GET"]),  # /user/ → user_info_json
        Route("/page", user_info_html, methods=["GET"]),  # /user/page → user_info_html
        Route(
            "/revoke", revoke_session, methods=["POST"], name="revoke_session_endpoint"
        ),  # /user/revoke → revoke_session
    ]

    browser_app = Starlette(routes=browser_routes)
    browser_app.add_middleware(
        AuthenticationMiddleware,
        backend=SessionAuthBackend(oauth_enabled=oauth_enabled),
    )

    # Mount browser app at /user (so /user and /user/page work)
    routes.append(Mount("/user", app=browser_app))
    logger.info("User info routes with session auth: /user, /user/page")

    # Mount FastMCP at root last (catch-all, handles OAuth via token_verifier)
    routes.append(Mount("/", app=mcp_app))

    app = Starlette(routes=routes, lifespan=starlette_lifespan)
    logger.info(
        "Routes: /user/* with SessionAuth, /mcp with FastMCP OAuth Bearer tokens"
    )

    # Add debugging middleware to log Authorization headers
    @app.middleware("http")
    async def log_auth_headers(request, call_next):
        auth_header = request.headers.get("authorization")
        if request.url.path.startswith("/mcp"):
            if auth_header:
                # Log first 50 chars of token for debugging
                token_preview = (
                    auth_header[:50] + "..." if len(auth_header) > 50 else auth_header
                )
                logger.info(f"🔑 /mcp request with Authorization: {token_preview}")
            else:
                logger.warning(
                    f"⚠️  /mcp request WITHOUT Authorization header from {request.client}"
                )
        response = await call_next(request)
        return response

    # Add CORS middleware to allow browser-based clients like MCP Inspector
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allow all origins for development
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # Add exception handler for scope challenges (OAuth mode only)
    if oauth_enabled:

        @app.exception_handler(InsufficientScopeError)
        async def handle_insufficient_scope(request, exc: InsufficientScopeError):
            """Return 403 with WWW-Authenticate header for scope challenges."""
            resource_url = os.getenv(
                "NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000"
            )
            scope_str = " ".join(exc.missing_scopes)

            return JSONResponse(
                status_code=403,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer error="insufficient_scope", '
                        f'scope="{scope_str}", '
                        f'resource_metadata="{resource_url}/.well-known/oauth-protected-resource/mcp"'
                    )
                },
                content={
                    "error": "insufficient_scope",
                    "scopes_required": exc.missing_scopes,
                },
            )

        logger.info("WWW-Authenticate scope challenge handler enabled")

    return app


@click.command()
@click.option(
    "--host", "-h", default="127.0.0.1", show_default=True, help="Server host"
)
@click.option(
    "--port", "-p", type=int, default=8000, show_default=True, help="Server port"
)
@click.option(
    "--log-level",
    "-l",
    default="info",
    show_default=True,
    type=click.Choice(["critical", "error", "warning", "info", "debug", "trace"]),
    help="Logging level",
)
@click.option(
    "--transport",
    "-t",
    default="sse",
    show_default=True,
    type=click.Choice(["sse", "streamable-http", "http"]),
    help="MCP transport protocol",
)
@click.option(
    "--enable-app",
    "-e",
    multiple=True,
    type=click.Choice(
        ["notes", "tables", "webdav", "calendar", "contacts", "cookbook", "deck"]
    ),
    help="Enable specific Nextcloud app APIs. Can be specified multiple times. If not specified, all apps are enabled.",
)
@click.option(
    "--oauth/--no-oauth",
    default=None,
    help="Force OAuth mode (if enabled) or BasicAuth mode (if disabled). By default, auto-detected based on environment variables.",
)
@click.option(
    "--oauth-client-id",
    envvar="NEXTCLOUD_OIDC_CLIENT_ID",
    help="OAuth client ID (can also use NEXTCLOUD_OIDC_CLIENT_ID env var)",
)
@click.option(
    "--oauth-client-secret",
    envvar="NEXTCLOUD_OIDC_CLIENT_SECRET",
    help="OAuth client secret (can also use NEXTCLOUD_OIDC_CLIENT_SECRET env var)",
)
@click.option(
    "--mcp-server-url",
    envvar="NEXTCLOUD_MCP_SERVER_URL",
    default="http://localhost:8000",
    show_default=True,
    help="MCP server URL for OAuth callbacks (can also use NEXTCLOUD_MCP_SERVER_URL env var)",
)
@click.option(
    "--nextcloud-host",
    envvar="NEXTCLOUD_HOST",
    help="Nextcloud instance URL (can also use NEXTCLOUD_HOST env var)",
)
@click.option(
    "--nextcloud-username",
    envvar="NEXTCLOUD_USERNAME",
    help="Nextcloud username for BasicAuth (can also use NEXTCLOUD_USERNAME env var)",
)
@click.option(
    "--nextcloud-password",
    envvar="NEXTCLOUD_PASSWORD",
    help="Nextcloud password for BasicAuth (can also use NEXTCLOUD_PASSWORD env var)",
)
@click.option(
    "--oauth-scopes",
    envvar="NEXTCLOUD_OIDC_SCOPES",
    default="openid profile email notes:read notes:write calendar:read calendar:write todo:read todo:write contacts:read contacts:write cookbook:read cookbook:write deck:read deck:write tables:read tables:write files:read files:write sharing:read sharing:write",
    show_default=True,
    help="OAuth scopes to request during client registration. These define the maximum allowed scopes for the client. Note: Actual supported scopes are discovered dynamically from MCP tools at runtime. (can also use NEXTCLOUD_OIDC_SCOPES env var)",
)
@click.option(
    "--oauth-token-type",
    envvar="NEXTCLOUD_OIDC_TOKEN_TYPE",
    default="bearer",
    show_default=True,
    type=click.Choice(["bearer", "jwt"], case_sensitive=False),
    help="OAuth token type (can also use NEXTCLOUD_OIDC_TOKEN_TYPE env var)",
)
@click.option(
    "--public-issuer-url",
    envvar="NEXTCLOUD_PUBLIC_ISSUER_URL",
    help="Public issuer URL for OAuth (can also use NEXTCLOUD_PUBLIC_ISSUER_URL env var)",
)
def run(
    host: str,
    port: int,
    log_level: str,
    transport: str,
    enable_app: tuple[str, ...],
    oauth: bool | None,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
    mcp_server_url: str,
    nextcloud_host: str | None,
    nextcloud_username: str | None,
    nextcloud_password: str | None,
    oauth_scopes: str,
    oauth_token_type: str,
    public_issuer_url: str | None,
):
    """
    Run the Nextcloud MCP server.

    \b
    Authentication Modes:
      - BasicAuth: Set NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD
      - OAuth: Leave USERNAME/PASSWORD unset (requires OIDC app enabled)

    \b
    Examples:
      # BasicAuth mode with CLI options
      $ nextcloud-mcp-server --nextcloud-host=https://cloud.example.com \\
          --nextcloud-username=admin --nextcloud-password=secret

      # BasicAuth mode with env vars (recommended for credentials)
      $ export NEXTCLOUD_HOST=https://cloud.example.com
      $ export NEXTCLOUD_USERNAME=admin
      $ export NEXTCLOUD_PASSWORD=secret
      $ nextcloud-mcp-server --host 0.0.0.0 --port 8000

      # OAuth mode with auto-registration
      $ nextcloud-mcp-server --nextcloud-host=https://cloud.example.com --oauth

      # OAuth mode with pre-configured client
      $ nextcloud-mcp-server --nextcloud-host=https://cloud.example.com --oauth \\
          --oauth-client-id=xxx --oauth-client-secret=yyy

      # OAuth mode with custom scopes and JWT tokens
      $ nextcloud-mcp-server --nextcloud-host=https://cloud.example.com --oauth \\
          --oauth-scopes="openid notes:read notes:write" --oauth-token-type=jwt

      # OAuth with public issuer URL (for Docker/proxy setups)
      $ nextcloud-mcp-server --nextcloud-host=http://app --oauth \\
          --public-issuer-url=http://localhost:8080
    """
    # Set env vars from CLI options if provided
    if nextcloud_host:
        os.environ["NEXTCLOUD_HOST"] = nextcloud_host
    if nextcloud_username:
        os.environ["NEXTCLOUD_USERNAME"] = nextcloud_username
    if nextcloud_password:
        os.environ["NEXTCLOUD_PASSWORD"] = nextcloud_password
    if oauth_client_id:
        os.environ["NEXTCLOUD_OIDC_CLIENT_ID"] = oauth_client_id
    if oauth_client_secret:
        os.environ["NEXTCLOUD_OIDC_CLIENT_SECRET"] = oauth_client_secret
    if oauth_scopes:
        os.environ["NEXTCLOUD_OIDC_SCOPES"] = oauth_scopes
    if oauth_token_type:
        os.environ["NEXTCLOUD_OIDC_TOKEN_TYPE"] = oauth_token_type
    if mcp_server_url:
        os.environ["NEXTCLOUD_MCP_SERVER_URL"] = mcp_server_url
    if public_issuer_url:
        os.environ["NEXTCLOUD_PUBLIC_ISSUER_URL"] = public_issuer_url

    # Force OAuth mode if explicitly requested
    if oauth is True:
        # Clear username/password to force OAuth mode
        if "NEXTCLOUD_USERNAME" in os.environ:
            click.echo(
                "Warning: --oauth flag set, ignoring NEXTCLOUD_USERNAME", err=True
            )
            del os.environ["NEXTCLOUD_USERNAME"]
        if "NEXTCLOUD_PASSWORD" in os.environ:
            click.echo(
                "Warning: --oauth flag set, ignoring NEXTCLOUD_PASSWORD", err=True
            )
            del os.environ["NEXTCLOUD_PASSWORD"]

        # Validate OAuth configuration
        nextcloud_host = os.getenv("NEXTCLOUD_HOST")
        if not nextcloud_host:
            raise click.ClickException(
                "OAuth mode requires NEXTCLOUD_HOST environment variable to be set"
            )

        # Check if we have client credentials OR if dynamic registration is possible
        has_client_creds = os.getenv("NEXTCLOUD_OIDC_CLIENT_ID") and os.getenv(
            "NEXTCLOUD_OIDC_CLIENT_SECRET"
        )

        if not has_client_creds:
            # No client credentials - will attempt dynamic registration
            # Show helpful message before server starts
            click.echo("", err=True)
            click.echo("OAuth Configuration:", err=True)
            click.echo("  Mode: Dynamic Client Registration", err=True)
            click.echo("  Host: " + nextcloud_host, err=True)
            click.echo("  Storage: SQLite (TOKEN_STORAGE_DB)", err=True)
            click.echo("", err=True)
            click.echo(
                "Note: Make sure 'Dynamic Client Registration' is enabled", err=True
            )
            click.echo("      in your Nextcloud OIDC app settings.", err=True)
            click.echo("", err=True)
        else:
            click.echo("", err=True)
            click.echo("OAuth Configuration:", err=True)
            click.echo("  Mode: Pre-configured Client", err=True)
            click.echo("  Host: " + nextcloud_host, err=True)
            click.echo(
                "  Client ID: "
                + os.getenv("NEXTCLOUD_OIDC_CLIENT_ID", "")[:16]
                + "...",
                err=True,
            )
            click.echo("", err=True)

    elif oauth is False:
        # Force BasicAuth mode - verify credentials exist
        if not os.getenv("NEXTCLOUD_USERNAME") or not os.getenv("NEXTCLOUD_PASSWORD"):
            raise click.ClickException(
                "--no-oauth flag set but NEXTCLOUD_USERNAME or NEXTCLOUD_PASSWORD not set"
            )

    enabled_apps = list(enable_app) if enable_app else None

    app = get_app(transport=transport, enabled_apps=enabled_apps)

    uvicorn.run(
        app=app, host=host, port=port, log_level=log_level, log_config=LOGGING_CONFIG
    )


if __name__ == "__main__":
    run()
