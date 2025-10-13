"""Helper functions for extracting OAuth context from MCP requests."""

import logging

from mcp.server.fastmcp import Context
from mcp.server.auth.provider import AccessToken

from ..client import NextcloudClient

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
