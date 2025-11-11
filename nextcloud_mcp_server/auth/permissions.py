"""Permission checking utilities for Nextcloud admin operations."""

import logging

from httpx import AsyncClient
from starlette.requests import Request

from nextcloud_mcp_server.client.users import UsersClient

logger = logging.getLogger(__name__)


async def is_nextcloud_admin(request: Request, http_client: AsyncClient) -> bool:
    """Check if the authenticated user is a Nextcloud administrator.

    This function extracts the username from the session/request context
    and checks if the user is a member of the "admin" group in Nextcloud.

    Args:
        request: Starlette request object with authenticated user
        http_client: Authenticated HTTP client for Nextcloud API calls

    Returns:
        True if user is admin, False otherwise

    Example:
        ```python
        if await is_nextcloud_admin(request, http_client):
            # Show admin-only features
            pass
        ```
    """
    try:
        # Extract username from authenticated session
        username = request.user.display_name
        if not username:
            logger.warning("No username found in authenticated session")
            return False

        # Query Nextcloud for user's group memberships
        users_client = UsersClient(http_client, username)
        user_groups = await users_client.get_user_groups(username)

        # Check if user is in the admin group
        is_admin = "admin" in user_groups
        logger.debug(
            f"Admin check for user '{username}': {is_admin} (groups: {user_groups})"
        )

        return is_admin

    except Exception as e:
        logger.error(f"Error checking admin permissions: {e}", exc_info=True)
        return False
