"""Token utility functions for extracting user identity from MCP access tokens.

Extracted from server/oauth_tools.py to break circular import dependencies
between server/ and auth/ layers.
"""

import logging
import os

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import Context

from nextcloud_mcp_server.auth.userinfo_routes import _query_idp_userinfo

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


async def extract_user_id_from_token(ctx: Context) -> str:
    """Extract user_id from the MCP access token (Flow 1).

    Handles both JWT and opaque tokens:
    - JWT: Decode and extract 'sub' claim
    - Opaque: Call userinfo endpoint to get 'sub'

    Args:
        ctx: MCP context with access token

    Returns:
        user_id extracted from token, or "default_user" as fallback
    """
    # Use MCP SDK's get_access_token() which uses contextvars
    access_token: AccessToken | None = get_access_token()

    if not access_token or not access_token.token:
        logger.warning("  ✗ No access token found via get_access_token()")
        return "default_user"

    token = access_token.token
    is_jwt = "." in token and token.count(".") >= 2
    logger.info(f"  Token type: {'JWT' if is_jwt else 'Opaque'}")

    # Try JWT decode first
    if is_jwt:
        try:
            payload = jwt.decode(token, options={"verify_signature": False})
            user_id = payload.get("sub", "unknown")
            logger.info(f"  ✓ JWT decode successful: user_id={user_id}")
            return user_id
        except Exception as e:
            logger.error(f"  ✗ JWT decode failed: {type(e).__name__}: {e}")

    # Opaque token - call userinfo endpoint
    logger.info("  Opaque token detected, calling userinfo endpoint...")
    try:
        # Get userinfo endpoint from OIDC discovery
        oidc_discovery_uri = os.getenv(
            "OIDC_DISCOVERY_URI",
            "http://localhost:8080/.well-known/openid-configuration",
        )
        async with nextcloud_httpx_client() as http_client:
            discovery_response = await http_client.get(oidc_discovery_uri)
            discovery_response.raise_for_status()
            discovery = discovery_response.json()
            userinfo_endpoint = discovery.get("userinfo_endpoint")

        if userinfo_endpoint:
            userinfo = await _query_idp_userinfo(token, userinfo_endpoint)
            if userinfo:
                user_id = userinfo.get("sub", "unknown")
                logger.info(f"  ✓ Userinfo query successful: user_id={user_id}")
                return user_id
            else:
                logger.error("  ✗ Userinfo query failed")
        else:
            logger.error("  ✗ No userinfo_endpoint available")
    except Exception as e:
        logger.error(f"  ✗ Userinfo query failed: {type(e).__name__}: {e}")

    # Fallback
    logger.warning("  Using fallback user_id: default_user")
    return "default_user"
