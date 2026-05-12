"""
Provisioning decorator for ADR-004 (Offline Access Architecture).

This decorator ensures users have completed Flow 2 (Resource Provisioning)
before accessing Nextcloud resources when offline access is enabled.
"""

import functools
import logging
from typing import Callable

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import Context
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from nextcloud_mcp_server.auth.storage import get_shared_storage

logger = logging.getLogger(__name__)


def require_provisioning(func: Callable) -> Callable:
    """
    Decorator that checks if user has provisioned Nextcloud access (Flow 2).

    This decorator:
    1. Extracts user_id from the MCP token (Flow 1)
    2. Checks if user has completed Flow 2 provisioning
    3. Returns helpful error message if not provisioned
    4. Allows access if provisioned

    Usage:
        @mcp.tool()
        @require_provisioning
        async def list_notes(ctx: Context):
            # Tool implementation
            pass
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # Extract context from arguments
        ctx = None
        for arg in args:
            if isinstance(arg, Context):
                ctx = arg
                break
        if not ctx:
            ctx = kwargs.get("ctx")

        if not ctx:
            raise McpError(
                ErrorData(
                    code=-1,
                    message="Context not found - cannot verify provisioning",
                )
            )

        # Check if we're in BasicAuth mode - if so, skip provisioning check
        # In BasicAuth mode, there's no OAuth and no provisioning needed
        lifespan_ctx = ctx.request_context.lifespan_context
        if hasattr(lifespan_ctx, "client"):
            # BasicAuth mode - no provisioning needed, just proceed
            logger.debug("BasicAuth mode detected - skipping provisioning check")
            return await func(*args, **kwargs)

        # Offline access mode - check if user has completed Flow 2 provisioning
        # Read user_id from the verified AccessToken populated by
        # UnifiedTokenVerifier; no second decode of the raw JWT here.
        access_token = get_access_token()
        user_id = access_token.resource if access_token else None
        if user_id:
            logger.debug("Checking provisioning for user: %s", user_id)

        if not user_id:
            raise McpError(
                ErrorData(
                    code=-1,
                    message="Cannot determine user identity for provisioning check",
                )
            )

        # Check provisioning status — share the process-wide singleton
        # rather than initialising a new sqlite handle per tool call.
        storage = await get_shared_storage()

        refresh_data = await storage.get_refresh_token(user_id)

        if not refresh_data:
            # User has not completed Flow 2 - provide helpful error
            logger.info(
                "User %s attempted to use Nextcloud tool without provisioning", user_id
            )
            raise McpError(
                ErrorData(
                    code=-1,
                    message=(
                        "Nextcloud access not provisioned. "
                        "Please run the 'provision_nextcloud_access' tool first to authorize "
                        "the MCP server to access Nextcloud on your behalf. "
                        "This is a one-time setup required for security."
                    ),
                )
            )

        logger.debug(
            "User %s has provisioned access - proceeding with tool execution", user_id
        )

        # User has provisioned - allow access
        return await func(*args, **kwargs)

    return wrapper


def require_provisioning_or_suggest(func: Callable) -> Callable:
    """
    Softer version that suggests provisioning but doesn't block.

    This decorator:
    1. Checks provisioning status
    2. Logs a warning if not provisioned
    3. Still allows the function to proceed
    4. Can be used for read-only operations that might work without explicit provisioning

    Usage:
        @mcp.tool()
        @require_provisioning_or_suggest
        async def list_tools(ctx: Context):
            # Tool implementation
            pass
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # Extract context from arguments
        ctx = None
        for arg in args:
            if isinstance(arg, Context):
                ctx = arg
                break
        if not ctx:
            ctx = kwargs.get("ctx")

        if ctx:
            # Try to check provisioning status
            try:
                access_token = get_access_token()
                user_id = access_token.resource if access_token else None

                if user_id:
                    # Check provisioning status using the shared singleton.
                    storage = await get_shared_storage()

                    refresh_data = await storage.get_refresh_token(user_id)

                    if not refresh_data:
                        logger.info(
                            "User %s has not provisioned Nextcloud access. Some features may not work. Consider running 'provision_nextcloud_access' tool.",
                            user_id,
                        )
                    else:
                        logger.debug("User %s has provisioned access", user_id)

            except Exception as e:
                logger.debug("Could not check provisioning status: %s", e)

        # Always proceed with the function
        return await func(*args, **kwargs)

    return wrapper
