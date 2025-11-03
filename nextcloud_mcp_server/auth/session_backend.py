"""Session-based authentication backend for Starlette routes.

Provides browser-based authentication for admin UI routes, separate from
MCP's OAuth authentication flow.
"""

import logging
import os

from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)


class SessionAuthBackend(AuthenticationBackend):
    """Authentication backend using signed session cookies.

    For BasicAuth mode: Always authenticates as the configured user.
    For OAuth mode: Checks for valid session cookie with stored refresh token.
    """

    def __init__(self, oauth_enabled: bool = False):
        """Initialize session authentication backend.

        Args:
            oauth_enabled: Whether OAuth mode is enabled
        """
        self.oauth_enabled = oauth_enabled

    async def authenticate(
        self, conn: HTTPConnection
    ) -> tuple[AuthCredentials, SimpleUser] | None:
        """Authenticate the request based on session cookie or BasicAuth mode.

        Args:
            conn: HTTP connection

        Returns:
            Tuple of (credentials, user) if authenticated, None otherwise
        """
        # BasicAuth mode: Always authenticated as the configured user
        if not self.oauth_enabled:
            username = os.getenv("NEXTCLOUD_USERNAME", "admin")
            return AuthCredentials(["authenticated", "admin"]), SimpleUser(username)

        # OAuth mode: Check for session cookie
        session_id = conn.cookies.get("mcp_session")
        logger.info(
            f"Session authentication check - cookie present: {session_id is not None}, path: {conn.url.path}"
        )
        if not session_id:
            logger.info("No session cookie found - redirecting to login")
            return None

        logger.info(f"Found session cookie: {session_id[:16]}...")

        # Get OAuth context from app state
        oauth_context = getattr(conn.app.state, "oauth_context", None)
        if not oauth_context:
            logger.warning("OAuth context not available in app state")
            return None

        # Validate session
        storage = oauth_context.get("storage")
        if not storage:
            logger.warning("OAuth storage not available")
            return None

        try:
            # Check if user has refresh token (indicates logged-in session)
            logger.info(f"Looking up refresh token for session: {session_id[:16]}...")
            token_data = await storage.get_refresh_token(session_id)
            if not token_data:
                logger.warning(
                    f"No refresh token found for session {session_id[:16]}..."
                )
                return None

            # Session is valid - use session_id (which is user_id from ID token) as username
            username = session_id
            logger.info(f"✓ Session authenticated successfully: {username[:16]}...")

            return AuthCredentials(["authenticated"]), SimpleUser(username)

        except Exception as e:
            logger.warning(f"Session validation error: {e}")
            return None
