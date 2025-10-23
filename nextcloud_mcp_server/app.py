import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import click
import httpx
import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from nextcloud_mcp_server.auth import (
    InsufficientScopeError,
    NextcloudTokenVerifier,
    get_access_token_scopes,
    has_required_scopes,
    is_jwt_token,
)
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import LOGGING_CONFIG, setup_logging
from nextcloud_mcp_server.context import get_client as get_nextcloud_client
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

logger = logging.getLogger(__name__)


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


@dataclass
class OAuthAppContext:
    """Application context for OAuth mode."""

    nextcloud_host: str
    token_verifier: NextcloudTokenVerifier


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

    # Try loading from storage file
    storage_path = os.getenv(
        "NEXTCLOUD_OIDC_CLIENT_STORAGE", ".nextcloud_oauth_client.json"
    )
    from pathlib import Path

    from nextcloud_mcp_server.auth.client_registration import load_client_from_file

    client_info = load_client_from_file(Path(storage_path))

    if client_info:
        logger.info(
            f"Loaded OAuth client from storage: {client_info.client_id[:16]}..."
        )
        return (client_info.client_id, client_info.client_secret)

    # Try dynamic registration if available
    if registration_endpoint:
        logger.info("Dynamic client registration available")
        mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")
        redirect_uris = [f"{mcp_server_url}/oauth/callback"]

        # Get scopes from environment or use defaults
        scopes = os.getenv(
            "NEXTCLOUD_OIDC_SCOPES", "openid profile email nc:read nc:write"
        )
        logger.info(f"Requesting OAuth scopes: {scopes}")

        # Get token type from environment (Bearer or jwt)
        # Note: Must be lowercase "jwt" to match OIDC app's check
        token_type = os.getenv("NEXTCLOUD_OIDC_TOKEN_TYPE", "Bearer").lower()
        # Special case: "bearer" should remain capitalized for compatibility
        if token_type != "jwt":
            token_type = "Bearer"
        logger.info(f"Requesting token type: {token_type}")

        # Load or register client
        from nextcloud_mcp_server.auth.client_registration import (
            load_or_register_client,
        )

        client_info = await load_or_register_client(
            nextcloud_url=nextcloud_host,
            registration_endpoint=registration_endpoint,
            storage_path=storage_path,
            client_name="Nextcloud MCP Server",
            redirect_uris=redirect_uris,
            scopes=scopes,
            token_type=token_type,
        )

        logger.info(f"OAuth client ready: {client_info.client_id[:16]}...")
        return (client_info.client_id, client_info.client_secret)

    # No credentials available
    raise ValueError(
        "OAuth mode requires either:\n"
        "1. NEXTCLOUD_OIDC_CLIENT_ID and NEXTCLOUD_OIDC_CLIENT_SECRET environment variables, OR\n"
        "2. Pre-existing client credentials file at NEXTCLOUD_OIDC_CLIENT_STORAGE, OR\n"
        "3. Dynamic client registration enabled on Nextcloud OIDC app"
    )


@asynccontextmanager
async def app_lifespan_basic(server: FastMCP) -> AsyncIterator[AppContext]:
    """
    Manage application lifecycle for BasicAuth mode.

    Creates a single Nextcloud client with basic authentication
    that is shared across all requests.
    """
    logger.info("Starting MCP server in BasicAuth mode")
    logger.info("Creating Nextcloud client with BasicAuth")

    client = NextcloudClient.from_env()
    logger.info("Client initialization complete")

    try:
        yield AppContext(client=client)
    finally:
        logger.info("Shutting down BasicAuth mode")
        await client.close()


@asynccontextmanager
async def app_lifespan_oauth(server: FastMCP) -> AsyncIterator[OAuthAppContext]:
    """
    Manage application lifecycle for OAuth mode.

    Initializes OAuth client registration and token verifier.
    Does NOT create a Nextcloud client - clients are created per-request.
    """
    logger.info("Starting MCP server in OAuth mode")

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        raise ValueError("NEXTCLOUD_HOST environment variable is required")

    nextcloud_host = nextcloud_host.rstrip("/")

    # Get OAuth discovery endpoint
    discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"

    try:
        # Fetch OIDC discovery
        async with httpx.AsyncClient() as client:
            response = await client.get(discovery_url)
            response.raise_for_status()
            discovery = response.json()

        logger.info(f"OIDC discovery successful: {discovery_url}")

        # Extract endpoints
        userinfo_uri = discovery["userinfo_endpoint"]
        registration_endpoint = discovery.get("registration_endpoint")
        introspection_uri = discovery.get("introspection_endpoint")

        logger.info(f"Userinfo endpoint: {userinfo_uri}")
        if introspection_uri:
            logger.info(f"Introspection endpoint: {introspection_uri}")

        # Load OAuth client credentials
        client_id, client_secret = await load_oauth_client_credentials(
            nextcloud_host=nextcloud_host, registration_endpoint=registration_endpoint
        )

        # Create token verifier with introspection support
        token_verifier = NextcloudTokenVerifier(
            nextcloud_host=nextcloud_host,
            userinfo_uri=userinfo_uri,
            introspection_uri=introspection_uri,
            client_id=client_id,
            client_secret=client_secret,
        )

        logger.info("OAuth initialization complete")

        try:
            yield OAuthAppContext(
                nextcloud_host=nextcloud_host, token_verifier=token_verifier
            )
        finally:
            logger.info("Shutting down OAuth mode")
            await token_verifier.close()

    except Exception as e:
        logger.error(f"Failed to initialize OAuth mode: {e}")
        raise


async def setup_oauth_config():
    """
    Setup OAuth configuration by performing OIDC discovery and client registration.

    This is done synchronously before FastMCP initialization because FastMCP
    requires token_verifier at construction time.

    Returns:
        Tuple of (nextcloud_host, token_verifier, auth_settings)
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        raise ValueError(
            "NEXTCLOUD_HOST environment variable is required for OAuth mode"
        )

    nextcloud_host = nextcloud_host.rstrip("/")
    discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"

    logger.info(f"Performing OIDC discovery: {discovery_url}")

    # Fetch OIDC discovery
    async with httpx.AsyncClient() as client:
        response = await client.get(discovery_url)
        response.raise_for_status()
        discovery = response.json()

    logger.info("OIDC discovery successful")

    # Validate PKCE support
    validate_pkce_support(discovery, discovery_url)

    # Extract endpoints
    issuer = discovery["issuer"]
    userinfo_uri = discovery["userinfo_endpoint"]
    jwks_uri = discovery.get("jwks_uri")
    introspection_uri = discovery.get("introspection_endpoint")
    registration_endpoint = discovery.get("registration_endpoint")

    logger.info("OIDC endpoints discovered:")
    logger.info(f"  Issuer: {issuer}")
    logger.info(f"  Userinfo: {userinfo_uri}")
    logger.info(f"  JWKS: {jwks_uri}")
    if introspection_uri:
        logger.info(f"  Introspection: {introspection_uri}")

    # Allow override of public issuer URL for both client configuration and JWT validation
    # When clients access Nextcloud via a public URL (e.g., http://127.0.0.1:8080),
    # the OIDC app issues JWT tokens with that public URL in the 'iss' claim,
    # even though the MCP server accesses Nextcloud via an internal URL (e.g., http://app).
    # Therefore, we must validate JWT tokens against the public issuer, not the internal one.
    public_issuer = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL")
    if public_issuer:
        public_issuer = public_issuer.rstrip("/")
        logger.info(
            f"Using public issuer URL for clients and JWT validation: {public_issuer}"
        )
        # Use public issuer for both client configuration AND JWT validation
        issuer = public_issuer
        jwt_validation_issuer = public_issuer
    else:
        # Use discovered issuer for both
        jwt_validation_issuer = issuer

    # Load OAuth client credentials
    client_id, client_secret = await load_oauth_client_credentials(
        nextcloud_host=nextcloud_host, registration_endpoint=registration_endpoint
    )

    # Create token verifier with JWT support and introspection
    token_verifier = NextcloudTokenVerifier(
        nextcloud_host=nextcloud_host,
        userinfo_uri=userinfo_uri,
        jwks_uri=jwks_uri,  # Enable JWT verification if available
        issuer=jwt_validation_issuer,  # Use original issuer for JWT validation
        introspection_uri=introspection_uri,  # Enable introspection for opaque tokens
        client_id=client_id,
        client_secret=client_secret,
    )

    # Create auth settings
    mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")

    # Note: We don't set required_scopes here anymore.
    # Scopes are now advertised via PRM endpoint and enforced per-tool.
    # This allows dynamic tool filtering based on user's actual token scopes.
    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(mcp_server_url),
    )

    logger.info("OAuth configuration complete")

    return nextcloud_host, token_verifier, auth_settings


def get_app(transport: str = "sse", enabled_apps: list[str] | None = None):
    setup_logging()

    # Determine authentication mode
    oauth_enabled = is_oauth_mode()

    if oauth_enabled:
        logger.info("Configuring MCP server for OAuth mode")
        # Asynchronously get the OAuth configuration
        import asyncio

        _, token_verifier, auth_settings = asyncio.run(setup_oauth_config())
        mcp = FastMCP(
            "Nextcloud MCP",
            lifespan=app_lifespan_oauth,
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
        client = get_nextcloud_client(ctx)
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

    # Override list_tools to filter based on user's token scopes (OAuth mode only)
    if oauth_enabled:
        original_list_tools = mcp._tool_manager.list_tools

        def list_tools_filtered():
            """List tools filtered by user's token scopes (JWT tokens only)."""
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

            # Only filter for JWT tokens (opaque tokens show all tools)
            # JWT tokens have scopes embedded, so we can reliably filter
            # Opaque tokens may not have accurate scope information from introspection
            if is_jwt and user_scopes:
                allowed_tools = [
                    tool
                    for tool in all_tools
                    if has_required_scopes(tool.fn, user_scopes)
                ]
                logger.info(
                    f"✂️ JWT scope filtering: {len(allowed_tools)}/{len(all_tools)} tools "
                    f"available for scopes: {user_scopes}"
                )
            else:
                # Opaque token, BasicAuth mode, or no token - show all tools
                allowed_tools = all_tools
                reason = (
                    "opaque token (no filtering)"
                    if not is_jwt and user_scopes
                    else "no token/BasicAuth"
                )
                logger.info(f"📋 Showing all {len(all_tools)} tools ({reason})")

            # Return the Tool objects directly (they're already in the correct format)
            return allowed_tools

        # Replace the tool manager's list_tools method
        mcp._tool_manager.list_tools = list_tools_filtered
        logger.info("Dynamic tool filtering enabled for OAuth mode (JWT tokens only)")

    if transport == "sse":
        mcp_app = mcp.sse_app()
        lifespan = None
    elif transport in ("http", "streamable-http"):
        mcp_app = mcp.streamable_http_app()

        @asynccontextmanager
        async def lifespan(app: Starlette):
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(mcp.session_manager.run())
                yield

    # Add Protected Resource Metadata (PRM) endpoint for OAuth mode
    routes = []
    if oauth_enabled:

        def oauth_protected_resource_metadata(request):
            """RFC 8959 Protected Resource Metadata endpoint."""
            mcp_server_url = os.getenv(
                "NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000"
            )
            # Use PUBLIC_ISSUER_URL for authorization server since external clients
            # (like Claude) need the publicly accessible URL, not internal Docker URLs
            public_issuer_url = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL")
            if not public_issuer_url:
                # Fallback to NEXTCLOUD_HOST if PUBLIC_ISSUER_URL not set
                public_issuer_url = os.getenv("NEXTCLOUD_HOST", "")

            return JSONResponse(
                {
                    "resource": mcp_server_url,
                    "scopes_supported": ["nc:read", "nc:write"],
                    "authorization_servers": [public_issuer_url],
                    "bearer_methods_supported": ["header"],
                    "resource_signing_alg_values_supported": ["RS256"],
                }
            )

        routes.append(
            Route(
                "/.well-known/oauth-protected-resource",
                oauth_protected_resource_metadata,
                methods=["GET"],
            )
        )
        logger.info("Protected Resource Metadata (PRM) endpoint enabled")

    routes.append(Mount("/", app=mcp_app))
    app = Starlette(routes=routes, lifespan=lifespan)

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
                        f'resource_metadata="{resource_url}/.well-known/oauth-protected-resource"'
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
    "--oauth-storage-path",
    envvar="NEXTCLOUD_OIDC_CLIENT_STORAGE",
    default=".nextcloud_oauth_client.json",
    show_default=True,
    help="Path to store OAuth client credentials (can also use NEXTCLOUD_OIDC_CLIENT_STORAGE env var)",
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
    default="openid profile email nc:read nc:write",
    show_default=True,
    help="OAuth scopes to request (can also use NEXTCLOUD_OIDC_SCOPES env var)",
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
    oauth_storage_path: str,
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
          --oauth-scopes="openid nc:read" --oauth-token-type=jwt

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
    if oauth_storage_path:
        os.environ["NEXTCLOUD_OIDC_CLIENT_STORAGE"] = oauth_storage_path
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
            click.echo(
                "  Storage: "
                + os.getenv(
                    "NEXTCLOUD_OIDC_CLIENT_STORAGE", ".nextcloud_oauth_client.json"
                ),
                err=True,
            )
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
