"""Helper functions for accessing context in MCP tools."""

from mcp.server.fastmcp import Context

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings


async def get_client(ctx: Context) -> NextcloudClient:
    """
    Get the appropriate Nextcloud client based on authentication mode.

    This function handles three modes:
    1. BasicAuth mode: Returns shared client from lifespan context
    2. OAuth pass-through mode (ENABLE_TOKEN_EXCHANGE=false, default):
       Verifies Flow 1 token and passes it to Nextcloud
    3. OAuth token exchange mode (ENABLE_TOKEN_EXCHANGE=true):
       Exchanges Flow 1 token for ephemeral Nextcloud token via RFC 8693

    Note: Nextcloud doesn't support OAuth scopes natively. Scopes are enforced
    by the MCP server via @require_scopes decorator, not by the IdP.

    This function automatically detects the authentication mode by checking
    the type of the lifespan context.

    Args:
        ctx: MCP request context

    Returns:
        NextcloudClient configured for the current authentication mode

    Raises:
        AttributeError: If context doesn't contain expected data

    Example:
        ```python
        @mcp.tool()
        async def my_tool(ctx: Context):
            client = await get_client(ctx)
            return await client.capabilities()
        ```
    """
    settings = get_settings()
    lifespan_ctx = ctx.request_context.lifespan_context

    # BasicAuth mode - use shared client (no token exchange)
    if hasattr(lifespan_ctx, "client"):
        return lifespan_ctx.client

    # OAuth mode (has 'nextcloud_host' attribute)
    if hasattr(lifespan_ctx, "nextcloud_host"):
        # Check if token exchange is enabled
        if settings.enable_token_exchange:
            from nextcloud_mcp_server.auth.context_helper import (
                get_session_client_from_context,
            )

            # Token exchange mode: Exchange Flow 1 token for ephemeral Nextcloud token
            return await get_session_client_from_context(
                ctx, lifespan_ctx.nextcloud_host
            )
        else:
            # Pass-through mode (default): Verify and pass Flow 1 token to Nextcloud
            from nextcloud_mcp_server.auth import get_client_from_context

            return get_client_from_context(ctx, lifespan_ctx.nextcloud_host)

    # Unknown context type
    raise AttributeError(
        f"Lifespan context does not have 'client' or 'nextcloud_host' attribute. "
        f"Type: {type(lifespan_ctx)}"
    )
