"""Helper functions for accessing context in MCP tools."""

import logging

from httpx import BasicAuth
from mcp.server.fastmcp import Context

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import (
    DeploymentMode,
    get_deployment_mode,
    get_settings,
)

logger = logging.getLogger(__name__)


async def get_client(ctx: Context) -> NextcloudClient:
    """
    Get the appropriate Nextcloud client based on authentication mode.

    ADR-016 compliant implementation supporting three deployment modes:

    1. Smithery stateless mode (SMITHERY_DEPLOYMENT=true):
       Create client from session configuration (nextcloud_url, username, app_password)
       No persistent state - client created per-request from Smithery session config.

    2. BasicAuth mode: Returns shared client from lifespan context

    3. OAuth mode:
       a. Multi-audience mode (ENABLE_TOKEN_EXCHANGE=false, default):
          Token already contains both MCP and Nextcloud audiences - use directly
       b. Token exchange mode (ENABLE_TOKEN_EXCHANGE=true):
          Exchange MCP token for Nextcloud token via RFC 8693

    SECURITY: Token passthrough has been REMOVED. All OAuth modes validate
    proper token audiences per MCP Security Best Practices specification.

    Note: Nextcloud doesn't support OAuth scopes natively. Scopes are enforced
    by the MCP server via @require_scopes decorator, not by the IdP.

    This function automatically detects the authentication mode by checking
    the deployment mode and type of the lifespan context.

    Args:
        ctx: MCP request context

    Returns:
        NextcloudClient configured for the current authentication mode

    Raises:
        AttributeError: If context doesn't contain expected data
        ValueError: If Smithery mode but session config is missing required fields

    Example:
        ```python
        @mcp.tool()
        async def my_tool(ctx: Context):
            client = await get_client(ctx)
            return await client.capabilities()
        ```
    """
    deployment_mode = get_deployment_mode()

    # ADR-016: Smithery stateless mode - create client from session config
    if deployment_mode == DeploymentMode.SMITHERY_STATELESS:
        return _get_client_from_session_config(ctx)

    settings = get_settings()
    lifespan_ctx = ctx.request_context.lifespan_context

    # BasicAuth mode - use shared client (no token exchange)
    if hasattr(lifespan_ctx, "client"):
        return lifespan_ctx.client

    # OAuth mode (has 'nextcloud_host' attribute)
    if hasattr(lifespan_ctx, "nextcloud_host"):
        from nextcloud_mcp_server.auth.context_helper import (
            get_client_from_context,
            get_session_client_from_context,
        )

        if settings.enable_token_exchange:
            # Mode 2: Exchange MCP token for Nextcloud token
            # Token was validated to have MCP audience in UnifiedTokenVerifier
            # Now exchange it for Nextcloud audience
            return await get_session_client_from_context(
                ctx, lifespan_ctx.nextcloud_host
            )
        else:
            # Mode 1: Multi-audience token - use directly
            # Token was validated to have MCP audience in UnifiedTokenVerifier
            # Nextcloud will independently validate its own audience when receiving API calls
            return get_client_from_context(ctx, lifespan_ctx.nextcloud_host)

    # Unknown context type
    raise AttributeError(
        f"Lifespan context does not have 'client' or 'nextcloud_host' attribute. "
        f"Type: {type(lifespan_ctx)}"
    )


def _get_client_from_session_config(ctx: Context) -> NextcloudClient:
    """
    Create NextcloudClient from Smithery session configuration.

    ADR-016: In Smithery stateless mode, each request includes session config
    with the user's Nextcloud credentials. This function creates a fresh client
    for each request - no state is persisted between requests.

    Expected session config fields (from Smithery configSchema):
    - nextcloud_url: str - Nextcloud instance URL (required)
    - username: str - Nextcloud username (required)
    - app_password: str - Nextcloud app password (required)

    Args:
        ctx: MCP request context containing session_config

    Returns:
        NextcloudClient configured with session credentials

    Raises:
        ValueError: If required session config fields are missing
    """
    # Access session config from context
    # In Smithery mode, this is populated from URL parameters
    session_config = getattr(ctx, "session_config", None)

    if session_config is None:
        raise ValueError(
            "Session configuration required in Smithery mode. "
            "Ensure nextcloud_url, username, and app_password are provided."
        )

    # Extract required fields - support both dict and object access
    if isinstance(session_config, dict):
        nextcloud_url = session_config.get("nextcloud_url")
        username = session_config.get("username")
        app_password = session_config.get("app_password")
    else:
        nextcloud_url = getattr(session_config, "nextcloud_url", None)
        username = getattr(session_config, "username", None)
        app_password = getattr(session_config, "app_password", None)

    # Validate required fields
    missing_fields = []
    if not nextcloud_url:
        missing_fields.append("nextcloud_url")
    if not username:
        missing_fields.append("username")
    if not app_password:
        missing_fields.append("app_password")

    if missing_fields:
        raise ValueError(
            f"Missing required session config fields: {', '.join(missing_fields)}. "
            f"Configure these in the Smithery connection settings."
        )

    # Type assertions after validation (for type checker)
    # These are guaranteed to be str after the missing_fields check above
    assert nextcloud_url is not None
    assert username is not None
    assert app_password is not None

    # Validate URL format
    if not nextcloud_url.startswith(("http://", "https://")):
        raise ValueError(
            f"Invalid nextcloud_url: {nextcloud_url}. "
            f"Must start with http:// or https://"
        )

    logger.debug(f"Creating Smithery client for {nextcloud_url} as {username}")

    # Create client with session credentials using BasicAuth
    return NextcloudClient(
        base_url=nextcloud_url,
        username=username,
        auth=BasicAuth(username, app_password),
    )
