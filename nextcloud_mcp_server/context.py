"""Helper functions for accessing context in MCP tools."""

from mcp.server.fastmcp import Context

from nextcloud_mcp_server.client import NextcloudClient


def get_client(ctx: Context) -> NextcloudClient:
    """
    Get the appropriate Nextcloud client based on authentication mode.

    In BasicAuth mode, returns the shared client from lifespan context.
    In OAuth mode, creates a new client per-request using the OAuth context.

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
            client = get_client(ctx)
            return await client.capabilities()
        ```
    """
    lifespan_ctx = ctx.request_context.lifespan_context

    # Try BasicAuth mode first (has 'client' attribute)
    if hasattr(lifespan_ctx, "client"):
        return lifespan_ctx.client

    # OAuth mode (has 'nextcloud_host' attribute)
    if hasattr(lifespan_ctx, "nextcloud_host"):
        from nextcloud_mcp_server.auth import get_client_from_context

        return get_client_from_context(ctx, lifespan_ctx.nextcloud_host)

    # Unknown context type
    raise AttributeError(
        f"Lifespan context does not have 'client' or 'nextcloud_host' attribute. "
        f"Type: {type(lifespan_ctx)}"
    )
