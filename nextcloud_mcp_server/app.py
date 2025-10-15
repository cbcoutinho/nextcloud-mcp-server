import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import click
import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.routing import Mount

from nextcloud_mcp_server.auth import NextcloudTokenVerifier, load_or_register_client
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import setup_logging
from nextcloud_mcp_server.context import get_client as get_nextcloud_client
from nextcloud_mcp_server.server import (
    configure_calendar_tools,
    configure_contacts_tools,
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
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(discovery_url)
            response.raise_for_status()
            discovery = response.json()

        logger.info(f"OIDC discovery successful: {discovery_url}")

        # Extract endpoints
        userinfo_uri = discovery["userinfo_endpoint"]
        registration_endpoint = discovery.get("registration_endpoint")

        logger.info(f"Userinfo endpoint: {userinfo_uri}")

        # Handle client registration
        client_id = os.getenv("NEXTCLOUD_OIDC_CLIENT_ID")
        client_secret = os.getenv("NEXTCLOUD_OIDC_CLIENT_SECRET")
        storage_path = os.getenv(
            "NEXTCLOUD_OIDC_CLIENT_STORAGE", ".nextcloud_oauth_client.json"
        )

        if client_id and client_secret:
            logger.info("Using pre-configured OAuth client credentials")
        elif registration_endpoint:
            logger.info("Dynamic client registration available")
            mcp_server_url = os.getenv(
                "NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000"
            )
            redirect_uris = [f"{mcp_server_url}/oauth/callback"]

            # Load or register client
            client_info = await load_or_register_client(
                nextcloud_url=nextcloud_host,
                registration_endpoint=registration_endpoint,
                storage_path=storage_path,
                client_name="Nextcloud MCP Server",
                redirect_uris=redirect_uris,
            )

            logger.info(f"OAuth client ready: {client_info.client_id[:16]}...")
        else:
            raise ValueError(
                "OAuth mode requires either:\n"
                "1. NEXTCLOUD_OIDC_CLIENT_ID and NEXTCLOUD_OIDC_CLIENT_SECRET, OR\n"
                "2. Dynamic client registration enabled on Nextcloud OIDC app"
            )

        # Create token verifier
        token_verifier = NextcloudTokenVerifier(
            nextcloud_host=nextcloud_host, userinfo_uri=userinfo_uri
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
    import httpx

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
    registration_endpoint = discovery.get("registration_endpoint")

    # Allow override of public issuer URL for clients
    # (useful when MCP server accesses Nextcloud via internal URL
    # but needs to advertise a different URL to clients)
    public_issuer = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL")
    if public_issuer:
        public_issuer = public_issuer.rstrip("/")
        logger.info(f"Using public issuer URL for clients: {public_issuer}")
        issuer = public_issuer

    # Handle client registration
    client_id = os.getenv("NEXTCLOUD_OIDC_CLIENT_ID")
    client_secret = os.getenv("NEXTCLOUD_OIDC_CLIENT_SECRET")

    if client_id and client_secret:
        logger.info("Using pre-configured OAuth client credentials")
    elif registration_endpoint:
        logger.info("Dynamic client registration available")
        storage_path = os.getenv(
            "NEXTCLOUD_OIDC_CLIENT_STORAGE", ".nextcloud_oauth_client.json"
        )
        mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")
        redirect_uris = [f"{mcp_server_url}/oauth/callback"]

        # Load or register client
        client_info = await load_or_register_client(
            nextcloud_url=nextcloud_host,
            registration_endpoint=registration_endpoint,
            storage_path=storage_path,
            client_name="Nextcloud MCP Server",
            redirect_uris=redirect_uris,
        )

        logger.info(f"OAuth client ready: {client_info.client_id[:16]}...")
    else:
        raise ValueError(
            "OAuth mode requires either:\n"
            "1. NEXTCLOUD_OIDC_CLIENT_ID and NEXTCLOUD_OIDC_CLIENT_SECRET, OR\n"
            "2. Dynamic client registration enabled on Nextcloud OIDC app"
        )

    # Create token verifier
    token_verifier = NextcloudTokenVerifier(
        nextcloud_host=nextcloud_host, userinfo_uri=userinfo_uri
    )

    # Create auth settings
    mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")
    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(mcp_server_url),
        required_scopes=["openid", "profile"],
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

        nextcloud_host, token_verifier, auth_settings = asyncio.run(
            setup_oauth_config()
        )
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

    app = Starlette(routes=[Mount("/", app=mcp_app)], lifespan=lifespan)

    return app


@click.command()
@click.option(
    "--host", "-h", default="127.0.0.1", show_default=True, help="Server host"
)
@click.option(
    "--port", "-p", type=int, default=8000, show_default=True, help="Server port"
)
@click.option(
    "--workers", "-w", type=int, default=None, help="Number of worker processes"
)
@click.option("--reload", "-r", is_flag=True, help="Enable auto-reload")
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
    type=click.Choice(["notes", "tables", "webdav", "calendar", "contacts", "deck"]),
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
def run(
    host: str,
    port: int,
    workers: int,
    reload: bool,
    log_level: str,
    transport: str,
    enable_app: tuple[str, ...],
    oauth: bool | None,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
    oauth_storage_path: str,
    mcp_server_url: str,
):
    """
    Run the Nextcloud MCP server.

    \b
    Authentication Modes:
      - BasicAuth: Set NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD
      - OAuth: Leave USERNAME/PASSWORD unset (requires OIDC app enabled)

    \b
    Examples:
      # BasicAuth mode (legacy)
      $ nextcloud-mcp-server --host 0.0.0.0 --port 8000

      # OAuth mode with auto-registration
      $ nextcloud-mcp-server --oauth

      # OAuth mode with pre-configured client
      $ nextcloud-mcp-server --oauth --oauth-client-id=xxx --oauth-client-secret=yyy
    """
    # Set OAuth env vars from CLI options if provided
    if oauth_client_id:
        os.environ["NEXTCLOUD_OIDC_CLIENT_ID"] = oauth_client_id
    if oauth_client_secret:
        os.environ["NEXTCLOUD_OIDC_CLIENT_SECRET"] = oauth_client_secret
    if oauth_storage_path:
        os.environ["NEXTCLOUD_OIDC_CLIENT_STORAGE"] = oauth_storage_path
    if mcp_server_url:
        os.environ["NEXTCLOUD_MCP_SERVER_URL"] = mcp_server_url

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

    if reload or workers:
        app = "nextcloud_mcp_server.app:get_app"
        factory = True
    else:
        app = get_app(transport=transport, enabled_apps=enabled_apps)
        factory = False

    uvicorn.run(
        app=app,
        factory=factory,
        host=host,
        port=port,
        reload=reload,
        workers=workers,
        log_level=log_level,
    )


if __name__ == "__main__":
    run()
