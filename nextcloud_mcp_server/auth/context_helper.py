"""Helper functions for extracting OAuth context from MCP requests.

ADR-005 compliant implementation for multi-audience token mode.
"""

import logging

from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import Context

from ..client import NextcloudClient

logger = logging.getLogger(__name__)


def get_client_from_context(ctx: Context, base_url: str) -> NextcloudClient:
    """
    Create NextcloudClient for multi-audience mode (no exchange needed).

    ADR-005 Mode 1: Use multi-audience tokens directly.
    The UnifiedTokenVerifier validated MCP audience per RFC 7519.
    Nextcloud will independently validate its own audience.

    Args:
        ctx: MCP request context containing session info
        base_url: Nextcloud base URL

    Returns:
        NextcloudClient configured with multi-audience token

    Raises:
        AttributeError: If context doesn't contain expected OAuth session data
        ValueError: If username cannot be extracted from token
    """
    try:
        # Extract validated access token from MCP context
        if hasattr(ctx.request_context.request, "user") and hasattr(
            ctx.request_context.request.user, "access_token"
        ):
            access_token: AccessToken = ctx.request_context.request.user.access_token
            logger.debug("Retrieved multi-audience token from request.user")
        else:
            logger.error(
                "OAuth authentication failed: No access token found in request"
            )
            raise AttributeError("No access token found in OAuth request context")

        # Extract username from resource field (RFC 8707)
        # UnifiedTokenVerifier stored the username here during validation
        username = access_token.resource

        if not username:
            logger.error("No username found in access token resource field")
            raise ValueError("Username not available in OAuth token context")

        logger.debug(
            f"Creating NextcloudClient for user {username} with multi-audience token "
            f"(no exchange needed)"
        )

        # Token was validated to have MCP audience
        # Nextcloud will validate its own audience independently
        return NextcloudClient.from_token(
            base_url=base_url, token=access_token.token, username=username
        )

    except AttributeError as e:
        logger.error(f"Failed to extract OAuth context: {e}")
        logger.error("This may indicate the server is not running in OAuth mode")
        raise
