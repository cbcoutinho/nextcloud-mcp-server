import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

if TYPE_CHECKING:
    from nextcloud_mcp_server.auth.storage import RefreshTokenStorage


import anyio
import click
import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as StarletteScope

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
    DeploymentMode,
    get_deployment_mode,
    get_document_processor_config,
    get_settings,
)
from nextcloud_mcp_server.context import get_client as get_nextcloud_client
from nextcloud_mcp_server.document_processors import get_registry
from nextcloud_mcp_server.observability import (
    ObservabilityMiddleware,
    setup_metrics,
    setup_tracing,
)
from nextcloud_mcp_server.observability.metrics import (
    record_dependency_check,
    set_dependency_health,
)
from nextcloud_mcp_server.server import (
    configure_calendar_tools,
    configure_contacts_tools,
    configure_cookbook_tools,
    configure_deck_tools,
    configure_news_tools,
    configure_notes_tools,
    configure_semantic_tools,
    configure_sharing_tools,
    configure_tables_tools,
    configure_webdav_tools,
)
from nextcloud_mcp_server.server.oauth_tools import register_oauth_tools
from nextcloud_mcp_server.vector import processor_task, scanner_task

logger = logging.getLogger(__name__)
HTTPXClientInstrumentor().instrument()


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

    # Register PyMuPDF processor (high priority, local, no API required)
    if "pymupdf" in config["processors"]:
        pymupdf_config = config["processors"]["pymupdf"]
        try:
            from nextcloud_mcp_server.document_processors.pymupdf import (
                PyMuPDFProcessor,
            )

            processor = PyMuPDFProcessor(
                extract_images=pymupdf_config.get("extract_images", True),
                image_dir=pymupdf_config.get("image_dir"),
            )
            registry.register(processor, priority=15)  # Higher than unstructured
            logger.info(
                f"Registered PyMuPDF processor: extract_images={pymupdf_config.get('extract_images', True)}"
            )
            registered_count += 1
        except Exception as e:
            logger.warning(f"Failed to register PyMuPDF processor: {e}")

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
class VectorSyncState:
    """
    Module-level state for vector sync background tasks.

    This singleton bridges the Starlette server lifespan (where background tasks run)
    and FastMCP session lifespans (where MCP tools need access to the streams).
    """

    document_send_stream: Optional[MemoryObjectSendStream] = None
    document_receive_stream: Optional[MemoryObjectReceiveStream] = None
    shutdown_event: Optional[anyio.Event] = None
    scanner_wake_event: Optional[anyio.Event] = None


# Module-level singleton for vector sync state
_vector_sync_state = VectorSyncState()


@dataclass
class AppContext:
    """Application context for BasicAuth mode."""

    client: NextcloudClient
    storage: Optional["RefreshTokenStorage"] = None
    document_send_stream: Optional[MemoryObjectSendStream] = None
    document_receive_stream: Optional[MemoryObjectReceiveStream] = None
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


@dataclass
class SmitheryAppContext:
    """Application context for Smithery stateless mode.

    ADR-016: No shared client - clients created per-request from session config.
    """

    pass  # No shared state needed - everything comes from session config


# ADR-016: Smithery config schema for container runtime
# This schema is served at /.well-known/mcp-config for Smithery discovery
# See: https://smithery.ai/docs/build/session-config
SMITHERY_CONFIG_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "https://server.smithery.ai/nextcloud-mcp-server/.well-known/mcp-config",
    "title": "Nextcloud MCP Server Configuration",
    "description": "Configuration for connecting to your Nextcloud instance via app password authentication",
    "x-query-style": "flat",  # Our schema has no nested objects, so flat style works
    "type": "object",
    "required": ["nextcloud_url", "username", "app_password"],
    "properties": {
        "nextcloud_url": {
            "type": "string",
            "title": "Nextcloud URL",
            "description": "Your Nextcloud instance URL (e.g., https://cloud.example.com). Must be publicly accessible.",
            "pattern": "^https?://.+",
        },
        "username": {
            "type": "string",
            "title": "Username",
            "description": "Your Nextcloud username",
            "minLength": 1,
        },
        "app_password": {
            "type": "string",
            "title": "App Password",
            "description": "Nextcloud app password. Generate at Settings > Security > App passwords. Do NOT use your main password.",
            "minLength": 1,
        },
    },
    "additionalProperties": False,
}


# ADR-016: Context variable to hold Smithery session config per-request
# This is set by SmitheryConfigMiddleware and accessed in context.py
_smithery_session_config: ContextVar[dict[str, str] | None] = ContextVar(
    "smithery_session_config"
)
_smithery_session_config.set(None)  # Set initial value


def get_smithery_session_config() -> dict | None:
    """Get the current Smithery session config from context variable.

    Used by context.py to access config extracted from URL query parameters.
    """
    return _smithery_session_config.get()


class SmitheryConfigMiddleware:
    """Middleware to extract Smithery config from URL query parameters.

    ADR-016: For container runtime, Smithery passes configuration as URL query
    parameters to the /mcp endpoint. This middleware extracts those parameters
    and stores them in a context variable for access in tools.

    Configuration parameters:
    - nextcloud_url: Nextcloud instance URL
    - username: Nextcloud username
    - app_password: Nextcloud app password

    The extracted config is stored in a ContextVar and can be accessed via
    get_smithery_session_config() in context.py.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(
        self, scope: StarletteScope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] == "http":
            # Extract config from query parameters
            from urllib.parse import parse_qs

            query_string = scope.get("query_string", b"").decode("utf-8")
            params = parse_qs(query_string)

            # Build session config from query parameters
            # Smithery uses dot notation for nested objects, but our schema is flat
            session_config = {}
            for key in ["nextcloud_url", "username", "app_password"]:
                if key in params:
                    # parse_qs returns lists, take first value
                    session_config[key] = params[key][0]

            # Store in context variable for access by context.py
            if session_config:
                _smithery_session_config.set(session_config)
                logger.debug(
                    f"Smithery config extracted: nextcloud_url={session_config.get('nextcloud_url')}, "
                    f"username={session_config.get('username')}"
                )

        try:
            await self.app(scope, receive, send)
        finally:
            # Clear context variable after request
            _smithery_session_config.set(None)


@asynccontextmanager
async def app_lifespan_smithery(server: FastMCP) -> AsyncIterator[SmitheryAppContext]:
    """
    Manage application lifecycle for Smithery stateless mode.

    ADR-016: Minimal lifespan with no shared state.
    - No shared Nextcloud client (created per-request from session config)
    - No vector sync (disabled in Smithery mode)
    - No persistent storage (stateless deployment)
    - No document processors (not enabled in Smithery mode)
    """
    logger.info("Starting MCP server in Smithery stateless mode")
    logger.info("Clients will be created per-request from session config")

    try:
        yield SmitheryAppContext()
    finally:
        logger.info("Shutting down Smithery stateless mode")


def is_oauth_mode() -> bool:
    """
    Determine if OAuth mode should be used.

    OAuth mode is enabled when:
    - NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD are NOT set
    - AND we are NOT in Smithery stateless mode
    - Or explicitly enabled via configuration

    Returns:
        True if OAuth mode, False if BasicAuth mode
    """
    # ADR-016: Smithery stateless mode uses per-request BasicAuth from session config
    # It's not OAuth mode even though env credentials aren't set
    deployment_mode = get_deployment_mode()
    if deployment_mode == DeploymentMode.SMITHERY_STATELESS:
        logger.info(
            "BasicAuth mode (Smithery stateless - credentials from session config)"
        )
        return False

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
        from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

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
        dcr_scopes = "openid profile email notes:read notes:write calendar:read calendar:write todo:read todo:write contacts:read contacts:write cookbook:read cookbook:write deck:read deck:write tables:read tables:write files:read files:write sharing:read sharing:write news:read news:write"

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
        from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

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
    Manage application lifecycle for BasicAuth mode (FastMCP session lifespan).

    Creates a single Nextcloud client with basic authentication
    that is shared across all requests within a session.

    Note: Background tasks (scanner, processor) are started at server level
    in starlette_lifespan, not here. This lifespan runs per-session.
    """
    logger.info("Starting MCP session in BasicAuth mode")
    logger.info("Creating Nextcloud client with BasicAuth")

    client = NextcloudClient.from_env()
    logger.info("Client initialization complete")

    # Initialize persistent storage (for webhook tracking and future features)
    from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

    storage = RefreshTokenStorage.from_env()
    await storage.initialize()
    logger.info("Persistent storage initialized (webhook tracking enabled)")

    # Initialize document processors
    initialize_document_processors()

    # Yield client context - scanner runs at server level (starlette_lifespan)
    # Include vector sync state from module singleton (set by starlette_lifespan)
    try:
        yield AppContext(
            client=client,
            storage=storage,
            document_send_stream=_vector_sync_state.document_send_stream,
            document_receive_stream=_vector_sync_state.document_receive_stream,
            shutdown_event=_vector_sync_state.shutdown_event,
            scanner_wake_event=_vector_sync_state.scanner_wake_event,
        )
    finally:
        logger.info("Shutting down BasicAuth session")
        await client.close()


async def setup_oauth_config():
    """
    Setup OAuth configuration by performing OIDC discovery and client registration.

    Auto-detects OAuth provider mode:
    - Integrated mode: OIDC_DISCOVERY_URL points to NEXTCLOUD_HOST (or not set)
      → Nextcloud OIDC app provides both OAuth and API access
    - External IdP mode: OIDC_DISCOVERY_URL points to external provider
      → External IdP for OAuth, Nextcloud user_oidc validates tokens and provides API access

    Uses OIDC environment variables:
    - OIDC_DISCOVERY_URL: OIDC discovery endpoint (optional, defaults to NEXTCLOUD_HOST)
    - NEXTCLOUD_OIDC_CLIENT_ID / NEXTCLOUD_OIDC_CLIENT_SECRET: Static credentials (optional, uses DCR if not provided)
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

    # Rewrite discovered endpoint URLs from public issuer to internal host
    # This is needed when OIDC discovery returns public URLs (e.g., http://localhost:8080)
    # but the server needs to access them via internal docker network (e.g., http://app:80)
    from urllib.parse import urlparse

    issuer_parsed = urlparse(issuer)
    nextcloud_parsed = urlparse(nextcloud_host)
    issuer_base = f"{issuer_parsed.scheme}://{issuer_parsed.netloc}"
    nextcloud_base = f"{nextcloud_parsed.scheme}://{nextcloud_parsed.netloc}"

    if issuer_base != nextcloud_base:
        logger.info(f"Rewriting OIDC endpoints: {issuer_base} → {nextcloud_base}")

        def rewrite_url(url: str | None) -> str | None:
            if url and url.startswith(issuer_base):
                return url.replace(issuer_base, nextcloud_base, 1)
            return url

        userinfo_uri = rewrite_url(userinfo_uri) or userinfo_uri
        jwks_uri = rewrite_url(jwks_uri)
        introspection_uri = rewrite_url(introspection_uri)
        registration_endpoint = rewrite_url(registration_endpoint)

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

    # Use NEXTCLOUD_PUBLIC_ISSUER_URL for IdP detection when set
    # This handles the case where MCP server accesses Nextcloud via internal URL (http://app:80)
    # but the issuer in OIDC discovery is the public URL (http://localhost:8080)
    public_issuer_for_detection = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL")
    if public_issuer_for_detection:
        comparison_issuer = normalize_url(public_issuer_for_detection)
    else:
        comparison_issuer = nextcloud_normalized

    is_external_idp = not issuer_normalized.startswith(comparison_issuer)

    if is_external_idp:
        oauth_provider = "external"  # Could be Keycloak, Auth0, Okta, etc.
        logger.info(
            f"✓ Detected external IdP mode (issuer: {issuer} != Nextcloud: {nextcloud_host})"
        )
        logger.info("  Tokens will be validated via Nextcloud user_oidc app")
    else:
        oauth_provider = "nextcloud"
        logger.info("✓ Detected integrated mode (Nextcloud OIDC app)")

        # For integrated mode, rewrite OIDC endpoints to use internal URL
        # The discovery document returns external URLs (http://localhost:8080)
        # but the MCP server needs internal URLs (http://app:80) for backend requests
        if jwks_uri and not os.getenv("OIDC_JWKS_URI"):
            internal_jwks_uri = f"{nextcloud_host}/apps/oidc/jwks"
            logger.info(
                f"  Auto-rewriting JWKS URI for internal access: {jwks_uri} → {internal_jwks_uri}"
            )
            jwks_uri = internal_jwks_uri
        if introspection_uri and not os.getenv("OIDC_INTROSPECTION_URI"):
            internal_introspection_uri = f"{nextcloud_host}/apps/oidc/introspect"
            logger.info(
                f"  Auto-rewriting introspection URI for internal access: {introspection_uri} → {internal_introspection_uri}"
            )
            introspection_uri = internal_introspection_uri
        if userinfo_uri:
            internal_userinfo_uri = f"{nextcloud_host}/apps/oidc/userinfo"
            logger.info(
                f"  Auto-rewriting userinfo URI for internal access: {userinfo_uri} → {internal_userinfo_uri}"
            )
            userinfo_uri = internal_userinfo_uri

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
            from nextcloud_mcp_server.auth.storage import (
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
    client_id = os.getenv("NEXTCLOUD_OIDC_CLIENT_ID")
    client_secret = os.getenv("NEXTCLOUD_OIDC_CLIENT_SECRET")

    if client_id and client_secret:
        logger.info(f"Using static OIDC client credentials: {client_id}")
    elif registration_endpoint:
        logger.info(
            "NEXTCLOUD_OIDC_CLIENT_ID not set, attempting Dynamic Client Registration"
        )
        client_id, client_secret = await load_oauth_client_credentials(
            nextcloud_host=nextcloud_host, registration_endpoint=registration_endpoint
        )
    else:
        raise ValueError(
            "NEXTCLOUD_OIDC_CLIENT_ID and NEXTCLOUD_OIDC_CLIENT_SECRET environment variables are required "
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


def get_app(transport: str = "streamable-http", enabled_apps: list[str] | None = None):
    # Initialize observability (logging will be configured by uvicorn)
    settings = get_settings()

    # Setup Prometheus metrics (always enabled by default)
    if settings.metrics_enabled:
        setup_metrics(port=settings.metrics_port)
        logger.info(
            f"Prometheus metrics enabled on dedicated port {settings.metrics_port}"
        )

    # Setup OpenTelemetry tracing (optional)
    if settings.otel_exporter_otlp_endpoint:
        setup_tracing(
            service_name=settings.otel_service_name,
            otlp_endpoint=settings.otel_exporter_otlp_endpoint,
            otlp_verify_ssl=settings.otel_exporter_verify_ssl,
            sampling_rate=settings.otel_traces_sampler_arg,
        )
        logger.info(
            f"OpenTelemetry tracing enabled (endpoint: {settings.otel_exporter_otlp_endpoint})"
        )
    else:
        logger.info(
            "OpenTelemetry tracing disabled (set OTEL_EXPORTER_OTLP_ENDPOINT to enable)"
        )

    # Determine authentication mode and deployment mode
    oauth_enabled = is_oauth_mode()
    deployment_mode = get_deployment_mode()

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
            # Disable DNS rebinding protection for containerized deployments (k8s, Docker)
            # MCP 1.23+ auto-enables this for localhost, breaking k8s service DNS names
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
    else:
        # ADR-016: Use Smithery lifespan for stateless mode, BasicAuth otherwise
        if deployment_mode == DeploymentMode.SMITHERY_STATELESS:
            logger.info("Configuring MCP server for Smithery stateless mode")
            # json_response=True returns plain JSON-RPC instead of SSE format,
            # required for Smithery scanner compatibility
            mcp = FastMCP(
                "Nextcloud MCP",
                lifespan=app_lifespan_smithery,
                json_response=True,
                # Disable DNS rebinding protection for containerized deployments (k8s, Docker)
                # MCP 1.23+ auto-enables this for localhost, breaking k8s service DNS names
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=False
                ),
            )
        else:
            logger.info("Configuring MCP server for BasicAuth mode")
            mcp = FastMCP(
                "Nextcloud MCP",
                lifespan=app_lifespan_basic,
                # Disable DNS rebinding protection for containerized deployments (k8s, Docker)
                # MCP 1.23+ auto-enables this for localhost, breaking k8s service DNS names
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=False
                ),
            )

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
        "news": configure_news_tools,
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

    # Register semantic search tools (cross-app feature)
    # ADR-016: Skip in Smithery stateless mode (no vector database)
    settings = get_settings()
    deployment_mode = get_deployment_mode()
    if deployment_mode == DeploymentMode.SMITHERY_STATELESS:
        logger.info("Skipping semantic search tools (Smithery stateless mode)")
    elif settings.vector_sync_enabled:
        logger.info("Configuring semantic search tools (vector sync enabled)")
        configure_semantic_tools(mcp)
    else:
        logger.info("Skipping semantic search tools (VECTOR_SYNC_ENABLED not set)")

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

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def starlette_lifespan(app: Starlette):
        # Set OAuth context for OAuth login routes (ADR-004)
        if oauth_enabled:
            # Prepare OAuth config from setup_oauth_config closure variables
            mcp_server_url = os.getenv(
                "NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000"
            )
            nextcloud_resource_uri = os.getenv("NEXTCLOUD_RESOURCE_URI", nextcloud_host)
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
                if isinstance(route, Mount) and route.path == "/app":
                    route.app.state.oauth_context = oauth_context_dict
                    logger.info(
                        "OAuth context shared with browser_app for session auth"
                    )
                    break

            logger.info(
                f"OAuth context initialized for login routes (client_id={client_id[:16]}...)"
            )
        else:
            # BasicAuth mode - share storage with browser_app for webhook management
            from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

            storage = RefreshTokenStorage.from_env()
            await storage.initialize()

            app.state.storage = storage

            # Also share with browser_app for webhook routes
            for route in app.routes:
                if isinstance(route, Mount) and route.path == "/app":
                    route.app.state.storage = storage
                    logger.info(
                        "Storage shared with browser_app for webhook management"
                    )
                    break

        # Start background vector sync tasks (ADR-007)
        # Scanner runs at server-level (once), not per-session
        import anyio as anyio_module

        settings = get_settings()

        # Check if vector sync is enabled and determine the mode
        enable_offline_access_for_sync = os.getenv(
            "ENABLE_OFFLINE_ACCESS", "false"
        ).lower() in ("true", "1", "yes")
        encryption_key = os.getenv("TOKEN_ENCRYPTION_KEY")

        if settings.vector_sync_enabled and not oauth_enabled:
            # BasicAuth mode - single user sync
            logger.info("Starting background vector sync tasks for BasicAuth mode")

            # Get username from environment
            username = os.getenv("NEXTCLOUD_USERNAME")
            if not username:
                raise ValueError(
                    "NEXTCLOUD_USERNAME required for vector sync in BasicAuth mode"
                )

            # Create client for vector sync (server-level, not per-session)
            client = NextcloudClient.from_env()

            # Initialize Qdrant collection before starting background tasks
            logger.info("Initializing Qdrant collection...")
            from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

            try:
                await get_qdrant_client()  # Triggers collection creation if needed
                logger.info("Qdrant collection ready")
            except Exception as e:
                logger.error(f"Failed to initialize Qdrant collection: {e}")
                raise RuntimeError(
                    f"Cannot start vector sync - Qdrant initialization failed: {e}"
                ) from e

            # Initialize shared state
            send_stream, receive_stream = anyio_module.create_memory_object_stream(
                max_buffer_size=settings.vector_sync_queue_max_size
            )
            shutdown_event = anyio_module.Event()
            scanner_wake_event = anyio_module.Event()

            # Store in app state for access from routes (ADR-007)
            app.state.document_send_stream = send_stream
            app.state.document_receive_stream = receive_stream
            app.state.shutdown_event = shutdown_event
            app.state.scanner_wake_event = scanner_wake_event

            # Also store in module singleton for FastMCP session lifespans
            _vector_sync_state.document_send_stream = send_stream
            _vector_sync_state.document_receive_stream = receive_stream
            _vector_sync_state.shutdown_event = shutdown_event
            _vector_sync_state.scanner_wake_event = scanner_wake_event
            logger.info("Vector sync state stored in module singleton")

            # Also share with browser_app for /app route
            for route in app.routes:
                if isinstance(route, Mount) and route.path == "/app":
                    route.app.state.document_send_stream = send_stream
                    route.app.state.document_receive_stream = receive_stream
                    route.app.state.shutdown_event = shutdown_event
                    route.app.state.scanner_wake_event = scanner_wake_event
                    logger.info("Vector sync state shared with browser_app for /app")
                    break

            # Start background tasks using anyio TaskGroup
            async with anyio_module.create_task_group() as tg:
                # Start scanner task
                await tg.start(
                    scanner_task,
                    send_stream,
                    shutdown_event,
                    scanner_wake_event,
                    client,
                    username,
                )

                # Start processor pool (each gets a cloned receive stream)
                for i in range(settings.vector_sync_processor_workers):
                    await tg.start(
                        processor_task,
                        i,
                        receive_stream.clone(),
                        shutdown_event,
                        client,
                        username,
                    )

                logger.info(
                    f"Background sync tasks started: 1 scanner + "
                    f"{settings.vector_sync_processor_workers} processors"
                )

                # Run MCP session manager and yield
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(mcp.session_manager.run())
                    try:
                        yield
                    finally:
                        # Shutdown signal
                        logger.info("Shutting down background sync tasks")
                        shutdown_event.set()
                        await client.close()
                        # TaskGroup automatically cancels all tasks on exit

        elif (
            settings.vector_sync_enabled
            and oauth_enabled
            and enable_offline_access_for_sync
            and refresh_token_storage
            and encryption_key
        ):
            # OAuth mode with offline access - multi-user sync
            logger.info("Starting background vector sync tasks for OAuth mode")

            from nextcloud_mcp_server.auth.token_broker import TokenBrokerService
            from nextcloud_mcp_server.vector.oauth_sync import (
                oauth_processor_task,
                user_manager_task,
            )

            # Get OIDC discovery URL (same as used for OAuth setup)
            discovery_url = os.getenv(
                "OIDC_DISCOVERY_URL",
                f"{nextcloud_host}/.well-known/openid-configuration",
            )

            # Get client credentials from oauth_context (set by setup_oauth_config)
            # This includes credentials from DCR if dynamic registration was used
            # Use different variable names to avoid shadowing client_id/client_secret from outer scope
            oauth_ctx = getattr(app.state, "oauth_context", {})
            oauth_config = oauth_ctx.get("config", {})
            sync_client_id = oauth_config.get("client_id")
            sync_client_secret = oauth_config.get("client_secret")

            if not sync_client_id or not sync_client_secret:
                logger.error(
                    "Cannot start OAuth vector sync: client credentials not found in oauth_context"
                )
                raise ValueError("OAuth client credentials required for vector sync")

            # Create token broker for background operations
            # Note: storage handles encryption internally, no key needed here
            # Client credentials are needed for token refresh operations
            token_broker = TokenBrokerService(
                storage=refresh_token_storage,
                oidc_discovery_url=discovery_url,
                nextcloud_host=nextcloud_host,
                client_id=sync_client_id,
                client_secret=sync_client_secret,
            )

            # Initialize Qdrant collection before starting background tasks
            logger.info("Initializing Qdrant collection...")
            from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

            try:
                await get_qdrant_client()  # Triggers collection creation if needed
                logger.info("Qdrant collection ready")
            except Exception as e:
                logger.error(f"Failed to initialize Qdrant collection: {e}")
                raise RuntimeError(
                    f"Cannot start vector sync - Qdrant initialization failed: {e}"
                ) from e

            # Initialize shared state
            send_stream, receive_stream = anyio_module.create_memory_object_stream(
                max_buffer_size=settings.vector_sync_queue_max_size
            )
            shutdown_event = anyio_module.Event()
            scanner_wake_event = anyio_module.Event()

            # User state tracking for user manager
            user_states: dict = {}

            # Store in app state for access from routes (ADR-007)
            app.state.document_send_stream = send_stream
            app.state.document_receive_stream = receive_stream
            app.state.shutdown_event = shutdown_event
            app.state.scanner_wake_event = scanner_wake_event

            # Also store in module singleton for FastMCP session lifespans
            _vector_sync_state.document_send_stream = send_stream
            _vector_sync_state.document_receive_stream = receive_stream
            _vector_sync_state.shutdown_event = shutdown_event
            _vector_sync_state.scanner_wake_event = scanner_wake_event
            logger.info("Vector sync state stored in module singleton")

            # Also share with browser_app for /app route
            for route in app.routes:
                if isinstance(route, Mount) and route.path == "/app":
                    route.app.state.document_send_stream = send_stream
                    route.app.state.document_receive_stream = receive_stream
                    route.app.state.shutdown_event = shutdown_event
                    route.app.state.scanner_wake_event = scanner_wake_event
                    logger.info("Vector sync state shared with browser_app for /app")
                    break

            # Start background tasks using anyio TaskGroup
            async with anyio_module.create_task_group() as tg:
                # Start user manager task (supervises per-user scanners)
                await tg.start(
                    user_manager_task,
                    send_stream,
                    shutdown_event,
                    scanner_wake_event,
                    token_broker,
                    refresh_token_storage,
                    nextcloud_host,
                    user_states,
                    tg,
                )

                # Start processor pool (each gets a cloned receive stream)
                for i in range(settings.vector_sync_processor_workers):
                    await tg.start(
                        oauth_processor_task,
                        i,
                        receive_stream.clone(),
                        shutdown_event,
                        token_broker,
                        nextcloud_host,
                    )

                logger.info(
                    f"Background sync tasks started: 1 user manager + "
                    f"{settings.vector_sync_processor_workers} processors"
                )

                # Run MCP session manager and yield
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(mcp.session_manager.run())
                    try:
                        yield
                    finally:
                        # Shutdown signal
                        logger.info("Shutting down background sync tasks")
                        shutdown_event.set()
                        # Close token broker HTTP client
                        if token_broker._http_client:
                            await token_broker._http_client.aclose()
                        # TaskGroup automatically cancels all tasks on exit
        else:
            # No vector sync - just run MCP session manager
            if settings.vector_sync_enabled:
                # Log why vector sync is not starting
                if oauth_enabled and not enable_offline_access_for_sync:
                    logger.warning(
                        "Vector sync enabled but ENABLE_OFFLINE_ACCESS=false - "
                        "vector sync requires offline access in OAuth mode"
                    )
                elif oauth_enabled and not refresh_token_storage:
                    logger.warning(
                        "Vector sync enabled but refresh token storage not available"
                    )
                elif oauth_enabled and not encryption_key:
                    logger.warning(
                        "Vector sync enabled but TOKEN_ENCRYPTION_KEY not set"
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

        # Check Nextcloud host configuration and connectivity
        nextcloud_host = os.getenv("NEXTCLOUD_HOST")
        if nextcloud_host:
            checks["nextcloud_configured"] = "ok"
            # Try to connect to Nextcloud
            start_time = time.time()
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"{nextcloud_host}/status.php")
                    duration = time.time() - start_time
                    if response.status_code == 200:
                        checks["nextcloud_reachable"] = "ok"
                        set_dependency_health("nextcloud", True)
                    else:
                        checks["nextcloud_reachable"] = (
                            f"error: status {response.status_code}"
                        )
                        set_dependency_health("nextcloud", False)
                        is_ready = False
                    record_dependency_check("nextcloud", duration)
            except Exception as e:
                duration = time.time() - start_time
                checks["nextcloud_reachable"] = f"error: {str(e)}"
                set_dependency_health("nextcloud", False)
                record_dependency_check("nextcloud", duration)
                is_ready = False
        else:
            checks["nextcloud_configured"] = "error: NEXTCLOUD_HOST not set"
            set_dependency_health("nextcloud", False)
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

        # Check Qdrant status if using network mode (external Qdrant service)
        # In-memory and persistent modes use embedded Qdrant, no external service to check
        vector_sync_enabled = (
            os.getenv("VECTOR_SYNC_ENABLED", "false").lower() == "true"
        )
        qdrant_url = os.getenv("QDRANT_URL")  # Only set in network mode

        if vector_sync_enabled and qdrant_url:
            start_time = time.time()
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"{qdrant_url}/readyz")
                    duration = time.time() - start_time
                    if response.status_code == 200:
                        checks["qdrant"] = "ok"
                        set_dependency_health("qdrant", True)
                    else:
                        checks["qdrant"] = f"error: status {response.status_code}"
                        set_dependency_health("qdrant", False)
                        is_ready = False
                    record_dependency_check("qdrant", duration)
            except Exception as e:
                duration = time.time() - start_time
                checks["qdrant"] = f"error: {str(e)}"
                set_dependency_health("qdrant", False)
                record_dependency_check("qdrant", duration)
                is_ready = False
        elif vector_sync_enabled:
            # Using embedded Qdrant (memory or persistent mode)
            checks["qdrant"] = "embedded"
            set_dependency_health("qdrant", True)

        status_code = 200 if is_ready else 503
        return JSONResponse(
            {
                "status": "ready" if is_ready else "not_ready",
                "checks": checks,
            },
            status_code=status_code,
        )

    async def handle_nextcloud_webhook(request):
        """Test webhook endpoint to capture and log Nextcloud webhook payloads.

        This is a temporary endpoint for testing webhook schemas and payloads.
        It logs the full payload and returns 200 OK immediately.
        """
        import json

        try:
            payload = await request.json()
            logger.info("=" * 80)
            logger.info("🔔 Webhook received from Nextcloud:")
            logger.info(json.dumps(payload, indent=2, sort_keys=True))
            logger.info("=" * 80)

            return JSONResponse(
                {"status": "received", "timestamp": payload.get("time")},
                status_code=200,
            )
        except Exception as e:
            logger.error(f"❌ Failed to parse webhook payload: {e}")
            return JSONResponse(
                {"error": "invalid_payload", "message": str(e)}, status_code=400
            )

    # Add Protected Resource Metadata (PRM) endpoint for OAuth mode
    routes = []

    # Add health check routes (available in both OAuth and BasicAuth modes)
    routes.append(Route("/health/live", health_live, methods=["GET"]))
    routes.append(Route("/health/ready", health_ready, methods=["GET"]))
    logger.info("Health check endpoints enabled: /health/live, /health/ready")

    # Add test webhook endpoint (for development/testing)
    routes.append(
        Route("/webhooks/nextcloud", handle_nextcloud_webhook, methods=["POST"])
    )
    logger.info("Test webhook endpoint enabled: /webhooks/nextcloud")

    # Add management API endpoints for Nextcloud PHP app (OAuth mode only)
    if oauth_enabled:
        from nextcloud_mcp_server.api.management import (
            create_webhook,
            delete_webhook,
            get_chunk_context,
            get_installed_apps,
            get_server_status,
            get_user_session,
            get_vector_sync_status,
            list_webhooks,
            revoke_user_access,
            unified_search,
            vector_search,
        )

        routes.append(Route("/api/v1/status", get_server_status, methods=["GET"]))
        routes.append(
            Route(
                "/api/v1/vector-sync/status",
                get_vector_sync_status,
                methods=["GET"],
            )
        )
        routes.append(
            Route(
                "/api/v1/users/{user_id}/session",
                get_user_session,
                methods=["GET"],
            )
        )
        routes.append(
            Route(
                "/api/v1/users/{user_id}/revoke",
                revoke_user_access,
                methods=["POST"],
            )
        )
        routes.append(
            Route("/api/v1/vector-viz/search", vector_search, methods=["POST"])
        )
        routes.append(
            Route("/api/v1/chunk-context", get_chunk_context, methods=["GET"])
        )
        # ADR-018: Unified search endpoint for Nextcloud PHP app integration
        routes.append(Route("/api/v1/search", unified_search, methods=["POST"]))
        routes.append(Route("/api/v1/apps", get_installed_apps, methods=["GET"]))
        # Webhook management endpoints
        routes.append(Route("/api/v1/webhooks", list_webhooks, methods=["GET"]))
        routes.append(Route("/api/v1/webhooks", create_webhook, methods=["POST"]))
        routes.append(
            Route("/api/v1/webhooks/{webhook_id}", delete_webhook, methods=["DELETE"])
        )
        logger.info(
            "Management API endpoints enabled: /api/v1/status, /api/v1/vector-sync/status, "
            "/api/v1/users/{user_id}/session, /api/v1/users/{user_id}/revoke, "
            "/api/v1/vector-viz/search, /api/v1/search, /api/v1/apps, "
            "/api/v1/webhooks"
        )

    # ADR-016: Add Smithery well-known config endpoint for container runtime discovery
    if deployment_mode == DeploymentMode.SMITHERY_STATELESS:

        def smithery_mcp_config(request):
            """Smithery MCP configuration endpoint.

            Returns JSON Schema for Smithery's configuration UI.
            This endpoint is required for Smithery container runtime discovery.
            """
            return JSONResponse(SMITHERY_CONFIG_SCHEMA)

        routes.append(
            Route(
                "/.well-known/mcp-config",
                smithery_mcp_config,
                methods=["GET"],
            )
        )
        logger.info("Smithery config endpoint enabled: /.well-known/mcp-config")

    # Note: Metrics endpoint is NOT exposed on main HTTP port for security reasons.
    # Metrics are served on dedicated port via setup_metrics() (default: 9090)

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
    # ADR-016: Skip /app admin UI in Smithery stateless mode (no vector sync, webhooks)
    if deployment_mode != DeploymentMode.SMITHERY_STATELESS:
        # These require session authentication, so we wrap them in a separate app
        from nextcloud_mcp_server.auth.session_backend import SessionAuthBackend
        from nextcloud_mcp_server.auth.userinfo_routes import (
            revoke_session,
            user_info_html,
            vector_sync_status_fragment,
        )
        from nextcloud_mcp_server.auth.viz_routes import (
            chunk_context_endpoint,
            vector_visualization_html,
            vector_visualization_search,
        )
        from nextcloud_mcp_server.auth.webhook_routes import (
            disable_webhook_preset,
            enable_webhook_preset,
            webhook_management_pane,
        )

        # Create a separate Starlette app for browser routes that need session auth
        # This prevents SessionAuthBackend from interfering with FastMCP's OAuth
        browser_routes = [
            Route(
                "/", user_info_html, methods=["GET"]
            ),  # /app → user info with all tabs
            Route(
                "/revoke",
                revoke_session,
                methods=["POST"],
                name="revoke_session_endpoint",
            ),  # /app/revoke → revoke_session
            # Vector sync status fragment (htmx polling)
            Route(
                "/vector-sync/status",
                vector_sync_status_fragment,
                methods=["GET"],
            ),  # /app/vector-sync/status
            # Vector visualization routes
            Route(
                "/vector-viz", vector_visualization_html, methods=["GET"]
            ),  # /app/vector-viz
            Route(
                "/vector-viz/search",
                vector_visualization_search,
                methods=["GET"],
            ),  # /app/vector-viz/search
            Route(
                "/chunk-context",
                chunk_context_endpoint,
                methods=["GET"],
            ),  # /app/chunk-context
            # Webhook management routes (admin-only)
            Route(
                "/webhooks", webhook_management_pane, methods=["GET"]
            ),  # /app/webhooks
            Route(
                "/webhooks/enable/{preset_id:str}",
                enable_webhook_preset,
                methods=["POST"],
            ),
            Route(
                "/webhooks/disable/{preset_id:str}",
                disable_webhook_preset,
                methods=["DELETE"],
            ),
        ]

        # Add static files mount if directory exists
        static_dir = os.path.join(os.path.dirname(__file__), "auth", "static")
        if os.path.isdir(static_dir):
            browser_routes.append(
                Mount("/static", StaticFiles(directory=static_dir), name="static")
            )
            logger.info(f"Mounted static files from {static_dir}")

        browser_app = Starlette(routes=browser_routes)
        browser_app.add_middleware(
            AuthenticationMiddleware,  # type: ignore[invalid-argument-type]
            backend=SessionAuthBackend(oauth_enabled=oauth_enabled),
        )

        # Add redirect from /app to /app/ (Starlette requires trailing slash for mounted apps)
        routes.append(
            Route("/app", lambda request: RedirectResponse("/app/", status_code=307))
        )

        # Mount browser app at /app (webapp and admin routes)
        routes.append(Mount("/app", app=browser_app))
        logger.info("App routes with session auth: /app, /app/webhooks, /app/revoke")
    else:
        logger.info("Admin UI (/app) disabled in Smithery stateless mode")

    # Mount FastMCP at root last (catch-all, handles OAuth via token_verifier)
    routes.append(Mount("/", app=mcp_app))

    app = Starlette(routes=routes, lifespan=starlette_lifespan)
    logger.info(
        "Routes: /user/* with SessionAuth, /mcp with FastMCP OAuth Bearer tokens"
    )

    # Add debugging middleware to log Authorization headers and client capabilities
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
                # Only warn about missing Authorization in OAuth mode
                # In BasicAuth mode, /mcp requests without Authorization are expected
                if oauth_enabled:
                    logger.warning(
                        f"⚠️  /mcp request WITHOUT Authorization header from {request.client}"
                    )

            # Log client capabilities on initialize request
            if request.method == "POST":
                # Read body to check for initialize request
                # Starlette caches the body internally, so it's safe to read here
                body = await request.body()
                try:
                    import json

                    data = json.loads(body)
                    # Check if this is an initialize request
                    if data.get("method") == "initialize":
                        params = data.get("params", {})
                        capabilities = params.get("capabilities", {})
                        client_info = params.get("clientInfo", {})

                        logger.info(
                            f"🔌 MCP client connected: {client_info.get('name', 'unknown')} "
                            f"v{client_info.get('version', 'unknown')}"
                        )

                        # Log capabilities in a structured way
                        cap_summary = []
                        # Check for presence using 'in' not truthiness (empty dict {} counts as having capability)
                        if "roots" in capabilities:
                            cap_summary.append("roots")
                        if "sampling" in capabilities:
                            cap_summary.append("sampling")
                        if "experimental" in capabilities:
                            cap_summary.append(
                                f"experimental({len(capabilities['experimental'])} features)"
                            )

                        logger.info(
                            f"📋 Client capabilities: {', '.join(cap_summary) if cap_summary else 'none'}"
                        )
                        # Log full capabilities at INFO level to diagnose capability issues
                        logger.info(
                            f"Full capabilities JSON: {json.dumps(capabilities)}"
                        )
                except Exception as e:
                    # Don't fail the request if logging fails
                    logger.debug(
                        f"Failed to parse MCP request for capability logging: {e}"
                    )

        response = await call_next(request)
        return response

    # Add CORS middleware to allow browser-based clients like MCP Inspector
    app.add_middleware(
        CORSMiddleware,  # type: ignore[invalid-argument-type]
        allow_origins=["*"],  # Allow all origins for development
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # Add observability middleware (metrics + tracing)
    if settings.metrics_enabled or settings.otel_exporter_otlp_endpoint:
        app.add_middleware(ObservabilityMiddleware)  # type: ignore[invalid-argument-type]
        logger.info("Observability middleware enabled (metrics and/or tracing)")

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

    # ADR-016: Apply SmitheryConfigMiddleware in Smithery stateless mode
    # This must be the outermost middleware to extract config from URL query parameters
    # before any other middleware processes the request
    if deployment_mode == DeploymentMode.SMITHERY_STATELESS:
        app = SmitheryConfigMiddleware(app)
        logger.info("SmitheryConfigMiddleware enabled for query parameter config")

    return app
