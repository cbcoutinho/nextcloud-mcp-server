"""Helper functions for extracting OAuth context from MCP requests."""

import logging

from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import Context

from ..client import NextcloudClient
from ..config import get_settings
from .token_exchange import exchange_token_for_audience

logger = logging.getLogger(__name__)


def get_client_from_context(ctx: Context, base_url: str) -> NextcloudClient:
    """
    Extract authenticated user context from MCP request and create NextcloudClient.

    This function retrieves the OAuth access token from the MCP context,
    extracts the username from the token's resource field (where we stored it
    during token verification), and creates a NextcloudClient with bearer auth.

    Args:
        ctx: MCP request context containing session info
        base_url: Nextcloud base URL

    Returns:
        NextcloudClient configured with bearer token auth

    Raises:
        AttributeError: If context doesn't contain expected OAuth session data
        ValueError: If username cannot be extracted from token
    """
    try:
        # In Starlette with FastMCP OAuth, the authenticated user info is stored in request.user
        # The FastMCP auth middleware sets request.user to an AuthenticatedUser object
        # which contains the access_token
        if hasattr(ctx.request_context.request, "user") and hasattr(
            ctx.request_context.request.user, "access_token"
        ):
            access_token: AccessToken = ctx.request_context.request.user.access_token
            logger.debug("Retrieved access token from request.user for OAuth request")
        else:
            logger.error(
                "OAuth authentication failed: No access token found in request"
            )
            raise AttributeError("No access token found in OAuth request context")

        # Extract username from resource field (RFC 8707)
        # We stored the username here during token verification
        username = access_token.resource

        if not username:
            logger.error("No username found in access token resource field")
            raise ValueError("Username not available in OAuth token context")

        logger.debug(f"Creating OAuth NextcloudClient for user: {username}")

        # Create client with bearer token
        return NextcloudClient.from_token(
            base_url=base_url, token=access_token.token, username=username
        )

    except AttributeError as e:
        logger.error(f"Failed to extract OAuth context: {e}")
        logger.error("This may indicate the server is not running in OAuth mode")
        raise


async def get_session_client_from_context(
    ctx: Context, base_url: str
) -> NextcloudClient:
    """
    Create NextcloudClient using RFC 8693 token exchange for session operations.

    This implements the token exchange pattern where:
    1. Extract Flow 1 token from context (aud: "mcp-server")
    2. Exchange it for ephemeral Nextcloud token via RFC 8693
    3. Create client with delegated token (NOT stored)

    Note: Nextcloud doesn't support OAuth scopes natively. Scopes are enforced
    by the MCP server via @require_scopes decorator, not by the IdP. Therefore,
    we don't pass scopes to the token exchange - the MCP server already validated
    permissions before calling this function.

    Args:
        ctx: MCP request context containing session info
        base_url: Nextcloud base URL

    Returns:
        NextcloudClient configured with ephemeral delegated token

    Raises:
        AttributeError: If context doesn't contain expected OAuth session data
        RuntimeError: If token exchange fails
    """
    settings = get_settings()

    # Check if token exchange is enabled
    if not settings.enable_token_exchange:
        logger.info("Token exchange disabled, falling back to standard OAuth flow")
        return get_client_from_context(ctx, base_url)

    try:
        # Extract Flow 1 token from context
        if hasattr(ctx.request_context.request, "user") and hasattr(
            ctx.request_context.request.user, "access_token"
        ):
            access_token: AccessToken = ctx.request_context.request.user.access_token
            flow1_token = access_token.token
            username = access_token.resource  # Username stored during verification
            logger.debug(f"Retrieved Flow 1 token for user: {username}")
        else:
            logger.error("No Flow 1 token found in request context")
            raise AttributeError("No access token found in OAuth request context")

        if not username:
            logger.error("No username found in access token resource field")
            raise ValueError("Username not available in OAuth token context")

        logger.info("Exchanging client token for Nextcloud API token (pure RFC 8693)")

        # Perform pure RFC 8693 token exchange (no refresh tokens)
        # Note: We don't pass scopes since Nextcloud doesn't enforce them.
        # The MCP server's @require_scopes decorator handles authorization.
        exchanged_token, expires_in = await exchange_token_for_audience(
            subject_token=flow1_token,
            requested_audience="nextcloud",
            requested_scopes=None,  # Nextcloud doesn't support scopes
        )

        logger.info(f"Pure token exchange successful. Token expires in {expires_in}s")

        # Create client with exchanged token
        # This token is ephemeral (per-request) and NOT stored
        return NextcloudClient.from_token(
            base_url=base_url, token=exchanged_token, username=username
        )

    except AttributeError as e:
        logger.error(f"Failed to extract OAuth context: {e}")
        raise
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        raise RuntimeError(f"Token exchange required but failed: {e}") from e
