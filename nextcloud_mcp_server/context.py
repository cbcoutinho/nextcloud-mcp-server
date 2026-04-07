"""Helper functions for accessing context in MCP tools."""

import logging
from typing import Protocol, runtime_checkable

from httpx import BasicAuth
from mcp.server.fastmcp import Context

from nextcloud_mcp_server.auth.context_helper import get_client_from_context
from nextcloud_mcp_server.auth.scope_authorization import ProvisioningRequiredError
from nextcloud_mcp_server.auth.storage import get_shared_storage
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings

logger = logging.getLogger(__name__)


@runtime_checkable
class BasicAuthLifespanContext(Protocol):
    """Protocol for lifespan contexts that carry a shared NextcloudClient.

    Implemented by :class:`~nextcloud_mcp_server.stdio.StdioContext` and
    the single-user lifespan context in ``app.py``.
    """

    client: NextcloudClient


async def get_client(ctx: Context) -> NextcloudClient:
    """
    Get the appropriate Nextcloud client based on authentication mode.

    Supports the following deployment modes:

    1. BasicAuth mode: Returns shared client from lifespan context
    2. Login Flow v2: OAuth for MCP session, app password for Nextcloud API
    3. Multi-user BasicAuth: Credentials passed through from request headers
    4. OAuth multi-audience: Token contains both MCP and Nextcloud audiences

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

    # Multi-user BasicAuth pass-through mode - extract credentials from request
    if settings.enable_multi_user_basic_auth:
        return _get_client_from_basic_auth(ctx)

    lifespan_ctx = ctx.request_context.lifespan_context

    # Login Flow v2 multi-user mode: app password is REQUIRED for NC API access
    # OAuth token is only used for MCP session identity, not NC API calls
    if hasattr(lifespan_ctx, "nextcloud_host") and settings.enable_login_flow:
        return await _get_client_from_login_flow(ctx, lifespan_ctx.nextcloud_host)

    # BasicAuth mode - use shared client (no token exchange)
    if isinstance(lifespan_ctx, BasicAuthLifespanContext):
        return lifespan_ctx.client

    # OAuth multi-audience mode (has 'nextcloud_host' attribute)
    if hasattr(lifespan_ctx, "nextcloud_host"):
        # Token was validated to have MCP audience in UnifiedTokenVerifier
        # Nextcloud will independently validate its own audience when receiving API calls
        return get_client_from_context(ctx, lifespan_ctx.nextcloud_host)

    # Unknown context type
    raise AttributeError(
        f"Lifespan context does not have 'client' or 'nextcloud_host' attribute. "
        f"Type: {type(lifespan_ctx)}"
    )


def _get_client_from_basic_auth(ctx: Context) -> NextcloudClient:
    """
    Create NextcloudClient from BasicAuth credentials in request headers.

    For multi-user BasicAuth pass-through mode, this function extracts
    username/password from the Authorization: Basic header (stored by
    BasicAuthMiddleware) and creates a client that passes these credentials
    through to Nextcloud APIs.

    The credentials are NOT stored persistently - they exist only for the
    duration of this request (stateless).

    Args:
        ctx: MCP request context with basic_auth in request state

    Returns:
        NextcloudClient configured with BasicAuth credentials

    Raises:
        ValueError: If BasicAuth credentials not found in request or if
                   NEXTCLOUD_HOST is not configured
    """
    settings = get_settings()

    # Validate that NEXTCLOUD_HOST is configured
    if not settings.nextcloud_host:
        raise ValueError(
            "NEXTCLOUD_HOST environment variable must be set for multi-user BasicAuth mode"
        )

    # Extract BasicAuth credentials from request state (set by BasicAuthMiddleware)
    # Access scope through the request object
    scope = getattr(ctx.request_context.request, "scope", None)
    if scope is None:
        raise ValueError("Request scope not available in context")

    request_state = scope.get("state", {})
    basic_auth = request_state.get("basic_auth")

    if not basic_auth:
        raise ValueError(
            "BasicAuth credentials not found in request. "
            "Ensure Authorization: Basic header is provided with valid credentials."
        )

    username = basic_auth.get("username")
    password = basic_auth.get("password")

    if not username or not password:
        raise ValueError("Invalid BasicAuth credentials - missing username or password")

    logger.debug(
        f"Creating multi-user BasicAuth client for {settings.nextcloud_host} as {username}"
    )

    # Create client that passes BasicAuth credentials through to Nextcloud
    # settings.nextcloud_host is guaranteed to be str after the check above
    return NextcloudClient(
        base_url=settings.nextcloud_host,
        username=username,
        auth=BasicAuth(username, password),
    )


async def _get_client_from_login_flow(
    ctx: Context, nextcloud_host: str
) -> NextcloudClient:
    """Create NextcloudClient from stored Login Flow v2 app password.

    In Login Flow v2 mode, the OAuth token only provides MCP session identity.
    Nextcloud API calls always use the stored app password obtained via Login Flow v2.

    Args:
        ctx: MCP context (used to extract user identity)
        nextcloud_host: Nextcloud instance URL

    Returns:
        NextcloudClient with stored app password credentials

    Raises:
        ProvisioningRequiredError: If no stored app password exists
    """
    from nextcloud_mcp_server.auth.token_utils import (  # noqa: PLC0415
        extract_user_id_from_token,
    )

    user_id = await extract_user_id_from_token(ctx)
    if not user_id or user_id == "default_user":
        raise ProvisioningRequiredError(
            "Cannot determine user identity from MCP token."
        )

    storage = await get_shared_storage()

    app_data = await storage.get_app_password_with_scopes(user_id)
    if not app_data:
        raise ProvisioningRequiredError(
            "Nextcloud access not provisioned. "
            "Call nc_auth_provision_access to complete Login Flow."
        )

    username = app_data.get("username") or user_id

    logger.debug(f"Creating Login Flow v2 client for {nextcloud_host} as {username}")

    return NextcloudClient(
        base_url=nextcloud_host,
        username=username,
        auth=BasicAuth(username, app_data["app_password"]),
    )
