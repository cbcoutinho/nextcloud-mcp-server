"""Scope-based authorization for MCP tools."""

import logging
from functools import wraps
from typing import Callable

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.utilities.context_injection import find_context_parameter

logger = logging.getLogger(__name__)


class ScopeAuthorizationError(Exception):
    """Raised when a request lacks required scopes."""

    pass


class InsufficientScopeError(ScopeAuthorizationError):
    """Raised when request lacks required scopes (enables step-up auth).

    This exception triggers a 403 response with WWW-Authenticate header
    containing the missing scopes, allowing clients to perform step-up
    authorization to obtain additional permissions.
    """

    def __init__(self, missing_scopes: list[str], message: str | None = None):
        self.missing_scopes = missing_scopes
        super().__init__(
            message or f"Missing required scopes: {', '.join(missing_scopes)}"
        )


def require_scopes(*required_scopes: str):
    """
    Decorator to require specific OAuth scopes for MCP tool execution.

    This decorator:
    1. Stores scope requirements as function metadata (_required_scopes attribute)
    2. Checks that the access token contains all required scopes before execution
    3. Raises ScopeAuthorizationError if any required scope is missing

    The stored metadata enables dynamic tool filtering - tools can be hidden from
    users who lack the necessary scopes.

    Args:
        *required_scopes: Variable number of scope strings required (e.g., "notes:read", "notes:write")

    Returns:
        Decorated function that checks scopes before execution

    Example:
        ```python
        @mcp.tool()
        @require_scopes("notes:read")
        async def nc_notes_get_note(ctx: Context, note_id: int):
            # This tool requires the notes:read scope
            ...

        @mcp.tool()
        @require_scopes("notes:write")
        async def nc_notes_create_note(ctx: Context, ...):
            # This tool requires the notes:write scope
            ...
        ```

    Raises:
        ScopeAuthorizationError: If required scopes are not present in the access token
    """

    def decorator(func: Callable):
        # Store scope requirements as function metadata for dynamic filtering
        func._required_scopes = list(required_scopes)  # type: ignore

        # Find which parameter receives the Context (FastMCP injects it by name)
        context_param_name = find_context_parameter(func)

        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract context from kwargs (where FastMCP injected it)
            ctx: Context | None = (
                kwargs.get(context_param_name) if context_param_name else None
            )

            if ctx is None:
                # No context parameter found - likely BasicAuth mode
                # In BasicAuth mode, all operations are allowed
                logger.debug(
                    f"No context parameter for {func.__name__} - allowing (BasicAuth mode)"
                )
                return await func(*args, **kwargs)

            # Check if we're in OAuth mode (access token available)
            access_token: AccessToken | None = getattr(
                ctx.request_context, "access_token", None
            )

            if access_token is None:
                # Not in OAuth mode (BasicAuth or no auth)
                # In BasicAuth mode, all operations are allowed
                logger.debug(
                    f"No access token present for {func.__name__} - allowing (BasicAuth mode)"
                )
                return await func(*args, **kwargs)

            # Extract scopes from access token
            token_scopes = set(access_token.scopes or [])
            required_scopes_set = set(required_scopes)

            # Check if all required scopes are present
            missing_scopes = required_scopes_set - token_scopes
            if missing_scopes:
                error_msg = (
                    f"Access denied to {func.__name__}: "
                    f"Missing required scopes: {', '.join(sorted(missing_scopes))}. "
                    f"Token has scopes: {', '.join(sorted(token_scopes)) if token_scopes else 'none'}"
                )
                logger.warning(error_msg)
                raise InsufficientScopeError(list(missing_scopes), error_msg)

            # All required scopes present - allow execution
            logger.debug(
                f"Scope authorization passed for {func.__name__}: {required_scopes}"
            )
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def get_access_token_scopes(ctx: Context | None = None) -> set[str]:
    """
    Extract scopes from the authenticated user's access token.

    This function uses MCP SDK's contextvar to access the token, which works
    across all request types including list_tools.

    Args:
        ctx: FastMCP context object (unused, kept for compatibility)

    Returns:
        Set of scope strings, empty set if no token or no scopes
    """
    # Use MCP SDK's get_access_token() which uses contextvars
    # This works for all request types, including list_tools
    access_token: AccessToken | None = get_access_token()

    if access_token is None:
        logger.debug("No access token found in auth context (likely BasicAuth mode)")
        return set()

    scopes = set(access_token.scopes or [])
    logger.info(f"✅ Extracted scopes from access token: {scopes}")
    return scopes


def check_scopes(ctx: Context, *required_scopes: str) -> tuple[bool, set[str]]:
    """
    Check if the request context has all required scopes.

    Utility function for manual scope checking without decorator.

    Args:
        ctx: FastMCP context object
        *required_scopes: Variable number of required scope strings

    Returns:
        Tuple of (has_all_scopes: bool, missing_scopes: set[str])

    Example:
        ```python
        async def my_tool(ctx: Context):
            has_scopes, missing = check_scopes(ctx, "notes:read", "notes:write")
            if not has_scopes:
                # Handle missing scopes
                ...
        ```
    """
    token_scopes = get_access_token_scopes(ctx)

    # If no access token, assume BasicAuth mode (all operations allowed)
    if not token_scopes and getattr(ctx.request_context, "access_token", None) is None:
        return True, set()

    required_scopes_set = set(required_scopes)
    missing_scopes = required_scopes_set - token_scopes

    return len(missing_scopes) == 0, missing_scopes


def get_required_scopes(func: Callable) -> list[str]:
    """
    Extract required scopes from a function decorated with @require_scopes.

    Args:
        func: Function to check (may be decorated)

    Returns:
        List of required scope strings, empty list if no scopes required

    Example:
        ```python
        @require_scopes("notes:read", "notes:write")
        async def my_tool():
            pass

        scopes = get_required_scopes(my_tool)  # ["notes:read", "notes:write"]
        ```
    """
    return getattr(func, "_required_scopes", [])


def is_jwt_token() -> bool:
    """
    Check if the current access token is in JWT format.

    JWT tokens have 3 parts separated by dots (header.payload.signature).
    Opaque tokens are random strings without this structure.

    Returns:
        True if current token is JWT format, False if opaque or no token
    """
    access_token: AccessToken | None = get_access_token()

    if access_token is None:
        logger.debug("No access token found - not JWT")
        return False

    # JWT tokens have exactly 2 dots (3 parts)
    token_string = access_token.token
    is_jwt = "." in token_string and token_string.count(".") == 2

    logger.debug(f"Token format check: is_jwt={is_jwt}")
    return is_jwt


def has_required_scopes(func: Callable, user_scopes: set[str]) -> bool:
    """
    Check if a user has all scopes required by a function.

    Used for dynamic tool filtering - determines if a tool should be visible
    to a user based on their token scopes.

    Args:
        func: Function decorated with @require_scopes
        user_scopes: Set of scopes the user possesses

    Returns:
        True if user has all required scopes (or no scopes required), False otherwise

    Example:
        ```python
        @require_scopes("notes:write")
        async def create_note():
            pass

        user_scopes = {"notes:read", "notes:write"}
        can_see = has_required_scopes(create_note, user_scopes)  # True

        limited_user_scopes = {"notes:read"}
        can_see = has_required_scopes(create_note, limited_user_scopes)  # False
        ```
    """
    required = get_required_scopes(func)

    # No scopes required → always allow
    if not required:
        return True

    # Empty user_scopes but scopes required → deny
    if not user_scopes:
        return False

    # Check if user has all required scopes
    return set(required).issubset(user_scopes)


def discover_all_scopes(mcp) -> list[str]:
    """
    Dynamically discover all OAuth scopes required by registered MCP tools.

    This function inspects all registered tools and extracts their required scopes
    from the @require_scopes decorator metadata. It provides a single source of truth
    for available scopes based on the actual tool implementations.

    Args:
        mcp: FastMCP instance with registered tools

    Returns:
        Sorted list of unique scope strings, including base OIDC scopes

    Example:
        ```python
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("My Server")

        @mcp.tool()
        @require_scopes("notes:read")
        async def get_notes():
            pass

        @mcp.tool()
        @require_scopes("notes:write")
        async def create_note():
            pass

        scopes = discover_all_scopes(mcp)
        # Returns: ["notes:read", "notes:write", "openid", "profile", "email"]
        ```

    Note:
        - Base OIDC scopes (openid, profile, email) are always included
        - Scopes are deduplicated and sorted alphabetically
        - Only scopes from decorated tools are included
        - Must be called after tools are registered
    """
    # Start with base OIDC scopes that are always required
    all_scopes = {"openid", "profile", "email"}

    # Get all registered tools
    try:
        tools = mcp._tool_manager.list_tools()
    except AttributeError:
        logger.warning("FastMCP instance does not have _tool_manager attribute")
        return sorted(all_scopes)

    # Extract scopes from each tool
    for tool in tools:
        # Get the original function (tools have a .fn attribute)
        func = getattr(tool, "fn", None)
        if func is None:
            continue

        # Extract scopes using existing helper
        tool_scopes = get_required_scopes(func)
        all_scopes.update(tool_scopes)

    # Return sorted list of unique scopes
    return sorted(all_scopes)
