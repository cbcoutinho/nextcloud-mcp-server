"""Helper functions for extracting OAuth context from MCP requests.

ADR-005 compliant implementation with token exchange caching.
"""

import hashlib
import logging
import time

from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import Context

from ..client import NextcloudClient
from ..config import get_settings
from .token_exchange import exchange_token_for_audience

logger = logging.getLogger(__name__)

# Token exchange cache: token_hash -> (exchanged_token, expiry_timestamp)
_exchange_cache: dict[str, tuple[str, float]] = {}


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


async def get_session_client_from_context(
    ctx: Context, base_url: str
) -> NextcloudClient:
    """
    Create NextcloudClient using RFC 8693 token exchange with caching.

    ADR-005 Mode 2: Exchange MCP token for Nextcloud token via RFC 8693.

    This implements the token exchange pattern where:
    1. Extract MCP token from context (validated by UnifiedTokenVerifier)
    2. Check cache for existing exchanged token
    3. If not cached or expired, exchange via RFC 8693
    4. Cache the exchanged token to minimize exchange frequency
    5. Create client with exchanged token

    CRITICAL: This is where token exchange happens, NOT in the verifier.
    The verifier already validated the MCP audience; now we exchange for Nextcloud.

    Note: Nextcloud doesn't support OAuth scopes natively. Scopes are enforced
    by the MCP server via @require_scopes decorator, not by the IdP. Therefore,
    we don't pass scopes to the token exchange - the MCP server already validated
    permissions before calling this function.

    Args:
        ctx: MCP request context containing session info
        base_url: Nextcloud base URL

    Returns:
        NextcloudClient configured with ephemeral exchanged token

    Raises:
        AttributeError: If context doesn't contain expected OAuth session data
        RuntimeError: If token exchange fails
    """
    settings = get_settings()

    try:
        # Extract MCP token from context
        if hasattr(ctx.request_context.request, "user") and hasattr(
            ctx.request_context.request.user, "access_token"
        ):
            access_token: AccessToken = ctx.request_context.request.user.access_token
            mcp_token = access_token.token
            username = access_token.resource  # Username from UnifiedTokenVerifier
            logger.debug(f"Retrieved MCP token for user: {username}")
        else:
            logger.error("No MCP token found in request context")
            raise AttributeError("No access token found in OAuth request context")

        if not username:
            logger.error("No username found in access token resource field")
            raise ValueError("Username not available in OAuth token context")

        # Check cache for existing exchanged token
        cache_key = hashlib.sha256(mcp_token.encode()).hexdigest()
        if cache_key in _exchange_cache:
            cached_token, expiry = _exchange_cache[cache_key]
            if time.time() < expiry:
                logger.debug(
                    f"Using cached exchanged token (expires in {expiry - time.time():.1f}s)"
                )
                return NextcloudClient.from_token(
                    base_url=base_url, token=cached_token, username=username
                )
            else:
                logger.debug("Cached token expired, removing from cache")
                del _exchange_cache[cache_key]

        # Perform RFC 8693 token exchange
        logger.info(f"Exchanging MCP token for Nextcloud API token (user: {username})")

        # Exchange for Nextcloud resource URI audience
        exchanged_token, expires_in = await exchange_token_for_audience(
            subject_token=mcp_token,
            requested_audience=settings.nextcloud_resource_uri or "nextcloud",
            requested_scopes=None,  # Nextcloud doesn't support scopes
        )

        logger.info(f"Token exchange successful. Token expires in {expires_in}s")

        # Cache the exchanged token
        # Use the minimum of exchange TTL and configured cache TTL
        cache_ttl = min(expires_in, settings.token_exchange_cache_ttl)
        _exchange_cache[cache_key] = (exchanged_token, time.time() + cache_ttl)
        logger.debug(f"Cached exchanged token for {cache_ttl}s")

        # Clean up expired cache entries
        _cleanup_exchange_cache()

        # Create client with exchanged token
        return NextcloudClient.from_token(
            base_url=base_url, token=exchanged_token, username=username
        )

    except AttributeError as e:
        logger.error(f"Failed to extract OAuth context: {e}")
        raise
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        raise RuntimeError(f"Token exchange required but failed: {e}") from e


def _cleanup_exchange_cache():
    """Remove expired entries from the token exchange cache."""
    global _exchange_cache
    now = time.time()
    expired_keys = [k for k, (_, expiry) in _exchange_cache.items() if expiry <= now]
    for key in expired_keys:
        del _exchange_cache[key]
    if expired_keys:
        logger.debug(f"Cleaned up {len(expired_keys)} expired cache entries")


def clear_exchange_cache():
    """Clear the entire token exchange cache. Useful for testing."""
    global _exchange_cache
    _exchange_cache.clear()
    logger.debug("Token exchange cache cleared")
