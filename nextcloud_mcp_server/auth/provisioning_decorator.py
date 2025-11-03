"""
Provisioning decorator for ADR-004 Progressive Consent Architecture.

This decorator ensures users have completed Flow 2 (Resource Provisioning)
before accessing Nextcloud resources.
"""

import functools
import logging
from typing import Callable

from mcp.server.fastmcp import Context
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage

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

        # Progressive Consent mode - check if user has completed Flow 2 provisioning
        # Get user_id from authorization token
        user_id = None
        if hasattr(ctx, "authorization") and ctx.authorization:
            try:
                import jwt

                token = ctx.authorization.token
                payload = jwt.decode(token, options={"verify_signature": False})
                user_id = payload.get("sub")
                logger.debug(f"Checking provisioning for user: {user_id}")
            except Exception as e:
                logger.warning(f"Failed to extract user_id from token: {e}")

        if not user_id:
            raise McpError(
                ErrorData(
                    code=-1,
                    message="Cannot determine user identity for provisioning check",
                )
            )

        # Check provisioning status
        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

        refresh_data = await storage.get_refresh_token(user_id)

        if not refresh_data:
            # User has not completed Flow 2 - provide helpful error
            logger.info(
                f"User {user_id} attempted to use Nextcloud tool without provisioning"
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
            f"User {user_id} has provisioned access - proceeding with tool execution"
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
                # Get user_id from authorization token
                user_id = None
                if hasattr(ctx, "authorization") and ctx.authorization:
                    import jwt

                    token = ctx.authorization.token
                    payload = jwt.decode(token, options={"verify_signature": False})
                    user_id = payload.get("sub")

                if user_id:
                    # Check provisioning status
                    storage = RefreshTokenStorage.from_env()
                    await storage.initialize()

                    refresh_data = await storage.get_refresh_token(user_id)

                    if not refresh_data:
                        logger.info(
                            f"User {user_id} has not provisioned Nextcloud access. "
                            "Some features may not work. Consider running "
                            "'provision_nextcloud_access' tool."
                        )
                    else:
                        logger.debug(f"User {user_id} has provisioned access")

            except Exception as e:
                logger.debug(f"Could not check provisioning status: {e}")

        # Always proceed with the function
        return await func(*args, **kwargs)

    return wrapper
