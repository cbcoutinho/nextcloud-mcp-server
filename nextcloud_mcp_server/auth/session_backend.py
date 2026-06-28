"""Session-based authentication backend for Starlette routes.

Provides browser-based authentication for admin UI routes, separate from
MCP's OAuth authentication flow.
"""

import logging

from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.requests import HTTPConnection

from nextcloud_mcp_server.config import cfg

logger = logging.getLogger(__name__)


class SessionAuthBackend(AuthenticationBackend):
    """Authentication backend using signed session cookies.

    For BasicAuth mode: Always authenticates as the configured user.
    For OAuth mode: Checks for valid session cookie with stored refresh token.

    Behavior note — silent invalidation on refresh-token TTL expiry:
        The OAuth path requires *both* a live ``browser_sessions`` row and a
        live ``refresh_tokens`` row for the resolved user. Logout deletes
        both atomically, so a logged-out user always fails closed here.
        However, if the refresh token expires by TTL (without an explicit
        logout) the row is removed by ``get_refresh_token`` and the browser
        session becomes unusable — the user simply gets redirected to
        ``/oauth/login``. This is intentional defense-in-depth: the
        refresh-token check is what makes a leaked or stale browser cookie
        unusable after revocation. Do not relax this without first removing
        the cleanup invariant on logout (PR #758 round-4 review medium 2).
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

        This backend is only applied to browser routes (/user/*) via a separate
        Starlette app mount. FastMCP routes use their own OAuth Bearer token
        authentication.

        Args:
            conn: HTTP connection

        Returns:
            Tuple of (credentials, user) if authenticated, None otherwise
        """
        # BasicAuth mode: Always authenticated as the configured user
        if not self.oauth_enabled:
            username = cfg("NEXTCLOUD_USERNAME", "admin")
            return AuthCredentials(["authenticated", "admin"]), SimpleUser(username)

        # OAuth mode: opaque random session_id cookie -> user_id mapping.
        # Replaces the prior `mcp_session=<user_id>` cookie pattern (issue
        # #626 finding 2). The cookie value is no longer the user identity;
        # we look it up server-side and reject unknown / expired sessions.
        session_id = conn.cookies.get("mcp_session")
        if not session_id:
            logger.info("No session cookie found - redirecting to login")
            return None

        oauth_context = getattr(conn.app.state, "oauth_context", None)
        if not oauth_context:
            logger.warning("OAuth context not available in app state")
            return None

        storage = oauth_context.get("storage")
        if not storage:
            logger.warning("OAuth storage not available")
            return None

        try:
            user_id = await storage.get_browser_session_user(session_id)
            if not user_id:
                logger.info(
                    "Browser session not found or expired (sid=%s…)", session_id[:8]
                )
                return None

            # Defense-in-depth: only authenticate sessions for users that
            # actually have a refresh token persisted. Logout deletes both,
            # so an expired/revoked user state will fail closed here.
            token_data = await storage.get_refresh_token(user_id)
            if not token_data:
                logger.warning(
                    "Session %s… has no refresh token for user %s; rejecting",
                    session_id[:8],
                    user_id,
                )
                # Proactively evict the orphan so the table doesn't accumulate
                # rows that the auth check will keep rejecting until TTL
                # cleanup (PR #758 round-7 minor).
                try:
                    await storage.delete_browser_session(session_id)
                except Exception as e:
                    logger.warning(
                        "Failed to delete orphaned browser session %s…: %s",
                        session_id[:8],
                        e,
                    )
                return None

            return AuthCredentials(["authenticated"]), SimpleUser(user_id)

        except Exception as e:
            logger.warning("Session validation error: %s", e)
            return None
