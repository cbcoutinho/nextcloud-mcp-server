"""User info routes for the MCP server admin UI.

Provides browser-based endpoints to view information about the currently
authenticated user. Uses session-based authentication with OAuth flow.

For BasicAuth mode: Shows configured user info (no login needed).
For OAuth mode: Requires browser-based OAuth login to establish session.
"""

import logging
import os
from typing import Any

import httpx
from starlette.authentication import requires
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)


async def _get_authenticated_client_for_userinfo(request: Request) -> httpx.AsyncClient:
    """Get an authenticated HTTP client for user info page operations.

    Args:
        request: Starlette request object

    Returns:
        Authenticated httpx.AsyncClient
    """
    oauth_ctx = getattr(request.app.state, "oauth_context", None)

    # BasicAuth mode - use credentials from environment
    if not oauth_ctx:
        nextcloud_host = os.getenv("NEXTCLOUD_HOST")
        username = os.getenv("NEXTCLOUD_USERNAME")
        password = os.getenv("NEXTCLOUD_PASSWORD")

        if not all([nextcloud_host, username, password]):
            raise RuntimeError("BasicAuth credentials not configured")

        assert nextcloud_host is not None  # Type narrowing for type checker
        return httpx.AsyncClient(
            base_url=nextcloud_host,
            auth=(username, password),
            timeout=30.0,
        )

    # OAuth mode - get token from session
    storage = oauth_ctx.get("storage")
    session_id = request.cookies.get("mcp_session")

    if not storage or not session_id:
        raise RuntimeError("Session not found")

    token_data = await storage.get_refresh_token(session_id)
    if not token_data or "access_token" not in token_data:
        raise RuntimeError("No access token found in session")

    access_token = token_data["access_token"]
    nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

    if not nextcloud_host:
        raise RuntimeError("Nextcloud host not configured")

    return httpx.AsyncClient(
        base_url=nextcloud_host,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30.0,
    )


async def _get_processing_status(request: Request) -> dict[str, Any] | None:
    """Get vector sync processing status.

    Returns processing status information including indexed count, pending count,
    and sync status. Only available when VECTOR_SYNC_ENABLED=true.

    Args:
        request: Starlette request object

    Returns:
        Dictionary with processing status, or None if vector sync is disabled
        or components are unavailable:
        {
            "indexed_count": int,  # Number of documents in Qdrant
            "pending_count": int,  # Number of documents in queue
            "status": str,  # "syncing" or "idle"
        }
    """
    # Check if vector sync is enabled
    vector_sync_enabled = os.getenv("VECTOR_SYNC_ENABLED", "false").lower() == "true"
    if not vector_sync_enabled:
        return None

    try:
        # Get document receive stream from app state
        document_receive_stream = getattr(
            request.app.state, "document_receive_stream", None
        )
        if document_receive_stream is None:
            logger.debug("document_receive_stream not available in app state")
            return None

        # Get pending count from stream statistics
        stats = document_receive_stream.statistics()
        pending_count = stats.current_buffer_used

        # Get Qdrant client and query indexed count
        indexed_count = 0
        try:
            from nextcloud_mcp_server.config import get_settings
            from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

            settings = get_settings()
            qdrant_client = await get_qdrant_client()

            # Count documents in collection
            count_result = await qdrant_client.count(
                collection_name=settings.get_collection_name()
            )
            indexed_count = count_result.count

        except Exception as e:
            logger.warning(f"Failed to query Qdrant for indexed count: {e}")
            # Continue with indexed_count = 0

        # Determine status
        status = "syncing" if pending_count > 0 else "idle"

        return {
            "indexed_count": indexed_count,
            "pending_count": pending_count,
            "status": status,
        }

    except Exception as e:
        logger.error(f"Error getting processing status: {e}")
        return None


@requires("authenticated", redirect="oauth_login")
async def vector_sync_status_fragment(request: Request) -> HTMLResponse:
    """Vector sync status fragment endpoint - returns HTML fragment with current status.

    This endpoint is polled by htmx to provide real-time updates of vector sync processing
    status without requiring a full page refresh.

    Requires authentication via session cookie (redirects to oauth_login route if not authenticated).

    Args:
        request: Starlette request object

    Returns:
        HTML response with vector sync status table fragment
    """
    processing_status = await _get_processing_status(request)

    # If vector sync is disabled or unavailable, return empty fragment
    if not processing_status:
        return HTMLResponse(
            """
            <div id="vector-sync-status" hx-get="/app/vector-sync/status" hx-trigger="every 10s" hx-swap="innerHTML">
                <p style="color: #999;">Vector sync not available</p>
            </div>
            """
        )

    indexed_count = processing_status["indexed_count"]
    pending_count = processing_status["pending_count"]
    status = processing_status["status"]

    # Format numbers with commas for readability
    indexed_count_str = f"{indexed_count:,}"
    pending_count_str = f"{pending_count:,}"

    # Status badge color and text
    if status == "syncing":
        status_badge = (
            '<span style="color: #ff9800; font-weight: bold;">⟳ Syncing</span>'
        )
    else:
        status_badge = '<span style="color: #4caf50; font-weight: bold;">✓ Idle</span>'

    # Return inner content only (container div is in initial page render)
    html = f"""
    <h2>Vector Sync Status</h2>
    <table>
        <tr>
            <td><strong>Indexed Documents</strong></td>
            <td>{indexed_count_str}</td>
        </tr>
        <tr>
            <td><strong>Pending Documents</strong></td>
            <td>{pending_count_str}</td>
        </tr>
        <tr>
            <td><strong>Status</strong></td>
            <td>{status_badge}</td>
        </tr>
    </table>
    """

    return HTMLResponse(html)


async def _get_userinfo_endpoint(oauth_ctx: dict[str, Any]) -> str | None:
    """Get the correct userinfo endpoint based on OAuth mode.

    Args:
        oauth_ctx: OAuth context from app.state

    Returns:
        Userinfo endpoint URL, or None if unavailable
    """
    oauth_client = oauth_ctx.get("oauth_client")

    # External IdP mode (Keycloak): use oauth_client's userinfo endpoint
    if oauth_client:
        # Ensure discovery has been performed
        if not oauth_client.userinfo_endpoint:
            try:
                await oauth_client.discover()
            except Exception as e:
                logger.error(f"Failed to discover IdP endpoints: {e}")
                return None

        logger.debug(
            f"Using external IdP userinfo endpoint: {oauth_client.userinfo_endpoint}"
        )
        return oauth_client.userinfo_endpoint

    # Integrated mode (Nextcloud): query discovery document
    oauth_config = oauth_ctx.get("config")
    if not oauth_config:
        return None

    discovery_url = oauth_config.get("discovery_url")
    if not discovery_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(discovery_url)
            response.raise_for_status()
            discovery = response.json()
            userinfo_endpoint = discovery.get("userinfo_endpoint")

            if userinfo_endpoint:
                logger.debug(
                    f"Using Nextcloud userinfo endpoint from discovery: {userinfo_endpoint}"
                )
                return userinfo_endpoint

            logger.warning("No userinfo_endpoint in discovery document")
            return None

    except Exception as e:
        logger.error(f"Failed to query discovery document for userinfo endpoint: {e}")
        return None


async def _query_idp_userinfo(
    access_token_str: str, userinfo_uri: str
) -> dict[str, Any] | None:
    """Query the IdP's userinfo endpoint.

    Args:
        access_token_str: The access token string
        userinfo_uri: The userinfo endpoint URI

    Returns:
        User info dictionary from IdP, or None if query fails
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                userinfo_uri,
                headers={"Authorization": f"Bearer {access_token_str}"},
            )
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.warning(f"Failed to query IdP userinfo endpoint: {e}")
        return None


async def _get_user_info(request: Request) -> dict[str, Any]:
    """Get user information for the currently authenticated user.

    IMPORTANT: This function reads from cached profile data stored at login time.
    It does NOT perform token refresh or query the IdP on every request. The
    profile was cached once during oauth_login_callback and is displayed from
    storage thereafter.

    This is for BROWSER UI DISPLAY ONLY. Do not use this for authorization
    decisions or background job authentication.

    Args:
        request: Starlette request object (must be authenticated)

    Returns:
        Dictionary containing user information from cache
    """
    username = request.user.display_name
    oauth_ctx = getattr(request.app.state, "oauth_context", None)

    # BasicAuth mode
    if not oauth_ctx:
        return {
            "username": username,
            "auth_mode": "basic",
            "nextcloud_host": os.getenv("NEXTCLOUD_HOST", "unknown"),
        }

    # OAuth mode - read cached profile from browser session
    storage = oauth_ctx.get("storage")
    session_id = request.cookies.get("mcp_session")

    if not storage or not session_id:
        return {
            "error": "Session not found",
            "username": username,
            "auth_mode": "oauth",
        }

    try:
        # Check if background access was granted (refresh token exists)
        # This works for both Flow 2 (elicitation) and browser login
        token_data = await storage.get_refresh_token(session_id)
        background_access_granted = token_data is not None

        # Build background access details
        background_access_details = None
        if token_data:
            background_access_details = {
                "flow_type": token_data.get("flow_type", "unknown"),
                "provisioned_at": token_data.get("provisioned_at", "unknown"),
                "provisioning_client_id": token_data.get(
                    "provisioning_client_id", "N/A"
                ),
                "scopes": token_data.get("scopes", "N/A"),
                "token_audience": token_data.get("token_audience", "unknown"),
            }

        # Retrieve cached user profile (no token operations!)
        profile_data = await storage.get_user_profile(session_id)

        # Build user context
        user_context = {
            "username": username,  # From request.user.display_name (session_id)
            "auth_mode": "oauth",
            "session_id": session_id[:16] + "...",  # Truncated for security
            "background_access_granted": background_access_granted,
            "background_access_details": background_access_details,
        }

        # Include cached profile if available
        if profile_data:
            user_context["idp_profile"] = profile_data
            logger.debug(f"Loaded cached profile for {session_id[:16]}...")
        else:
            logger.warning(f"No cached profile found for {session_id[:16]}...")
            user_context["idp_profile_error"] = (
                "Profile not cached. Try logging out and back in."
            )

        return user_context

    except Exception as e:
        import traceback

        logger.error(f"Error retrieving user info: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {
            "error": f"Failed to retrieve user info: {e}",
            "username": username,
            "auth_mode": "oauth",
        }


@requires("authenticated", redirect="oauth_login")
async def user_info_json(request: Request) -> JSONResponse:
    """User info endpoint - returns JSON with current user information.

    Requires authentication via session cookie (redirects to oauth_login route if not authenticated).

    Args:
        request: Starlette request object

    Returns:
        JSON response with user information
    """
    user_info = await _get_user_info(request)
    return JSONResponse(user_info)


@requires("authenticated", redirect="oauth_login")
async def user_info_html(request: Request) -> HTMLResponse:
    """User info page - returns HTML with current user information.

    Requires authentication via session cookie (redirects to oauth_login route if not authenticated).

    Args:
        request: Starlette request object

    Returns:
        HTML response with formatted user information
    """
    user_context = await _get_user_info(request)

    # Get vector sync processing status
    processing_status = await _get_processing_status(request)

    # Check if user is admin (for Webhooks tab)
    is_admin = False
    try:
        from nextcloud_mcp_server.auth.permissions import is_nextcloud_admin

        # Get authenticated HTTP client
        http_client = await _get_authenticated_client_for_userinfo(request)
        is_admin = await is_nextcloud_admin(request, http_client)
        await http_client.aclose()
    except Exception as e:
        logger.warning(f"Failed to check admin status: {e}")
        # Default to not admin if check fails

    # Check for error
    if "error" in user_context and user_context["error"] != "":
        # Get login URL dynamically
        oauth_ctx = getattr(request.app.state, "oauth_context", None)
        login_url = str(request.url_for("oauth_login")) if oauth_ctx else "/oauth/login"

        error_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Error - Nextcloud MCP Server</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                    max-width: 800px;
                    margin: 50px auto;
                    padding: 20px;
                    background-color: #f5f5f5;
                }}
                .container {{
                    background: white;
                    border-radius: 8px;
                    padding: 30px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                }}
                h1 {{
                    color: #d32f2f;
                    margin-top: 0;
                }}
                .error {{
                    background-color: #ffebee;
                    border-left: 4px solid #d32f2f;
                    padding: 15px;
                    margin: 20px 0;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Error Retrieving User Info</h1>
                <div class="error">
                    <strong>Error:</strong> {user_context["error"]}
                </div>
                <p><a href="{login_url}">Login again</a></p>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=error_html)

    # Build HTML response
    auth_mode = user_context.get("auth_mode", "unknown")
    username = user_context.get("username", "unknown")

    # Get logout URL dynamically for OAuth mode
    logout_url = ""
    if auth_mode == "oauth":
        oauth_ctx = getattr(request.app.state, "oauth_context", None)
        logout_url = (
            str(request.url_for("oauth_logout")) if oauth_ctx else "/oauth/logout"
        )

    # Build host info HTML (BasicAuth only)
    host_info_html = ""
    if auth_mode == "basic":
        nextcloud_host = user_context.get("nextcloud_host", "unknown")
        host_info_html = f"""
        <h2>Connection</h2>
        <table>
            <tr>
                <td><strong>Nextcloud Host</strong></td>
                <td>{nextcloud_host}</td>
            </tr>
        </table>
        """

    # Build session info HTML (OAuth only)
    session_info_html = ""
    if auth_mode == "oauth" and "session_id" in user_context:
        session_id = user_context.get("session_id", "unknown")
        background_access_granted = user_context.get("background_access_granted", False)
        background_details = user_context.get("background_access_details")

        # Build background access section
        background_html = ""
        if background_access_granted and background_details:
            flow_type = background_details.get("flow_type", "unknown")
            provisioned_at = background_details.get("provisioned_at", "unknown")
            scopes = background_details.get("scopes", "N/A")
            token_audience = background_details.get("token_audience", "unknown")

            background_html = f"""
            <tr>
                <td><strong>Background Access</strong></td>
                <td><span style="color: #4caf50; font-weight: bold;">✓ Granted</span></td>
            </tr>
            <tr>
                <td><strong>Flow Type</strong></td>
                <td>{flow_type}</td>
            </tr>
            <tr>
                <td><strong>Provisioned At</strong></td>
                <td>{provisioned_at}</td>
            </tr>
            <tr>
                <td><strong>Token Audience</strong></td>
                <td>{token_audience}</td>
            </tr>
            <tr>
                <td><strong>Scopes</strong></td>
                <td><code style="font-size: 11px;">{scopes}</code></td>
            </tr>
            """
        else:
            background_html = """
            <tr>
                <td><strong>Background Access</strong></td>
                <td><span style="color: #999;">Not Granted</span></td>
            </tr>
            """

        session_info_html = f"""
        <h2>Session Information</h2>
        <table>
            <tr>
                <td><strong>Session ID</strong></td>
                <td><code>{session_id}</code></td>
            </tr>
            {background_html}
        </table>
        """

        # Add revoke button if background access is granted
        if background_access_granted:
            revoke_url = str(request.url_for("revoke_session_endpoint"))
            session_info_html += f"""
            <div style="margin-top: 15px;">
                <form method="post" action="{revoke_url}" onsubmit="return confirm('Are you sure you want to revoke background access? This will delete the refresh token.');">
                    <button type="submit" style="padding: 8px 16px; background-color: #ff9800; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 14px;">
                        Revoke Background Access
                    </button>
                </form>
            </div>
            """

    # Build vector sync status HTML (with htmx auto-refresh)
    vector_status_html = ""
    if processing_status:
        # Use htmx to load and auto-refresh the status fragment
        # Container div stays stable, only inner content updates every 10s
        vector_status_html = """
            <div id="vector-sync-status" hx-get="/app/vector-sync/status" hx-trigger="load, every 10s" hx-swap="innerHTML">
                <p style="color: #999;">Loading vector sync status...</p>
            </div>
        """

    # Build IdP profile HTML
    idp_profile_html = ""
    if "idp_profile" in user_context:
        idp_profile = user_context["idp_profile"]
        idp_profile_html = "<h2>Identity Provider Profile</h2><table>"
        for key, value in idp_profile.items():
            # Handle list values
            if isinstance(value, list):
                value_str = ", ".join(str(v) for v in value)
            else:
                value_str = str(value)
            idp_profile_html += f"""
            <tr>
                <td><strong>{key}</strong></td>
                <td>{value_str}</td>
            </tr>
            """
        idp_profile_html += "</table>"
    elif "idp_profile_error" in user_context:
        idp_profile_html = f"""
        <h2>Identity Provider Profile</h2>
        <div class="warning">{user_context["idp_profile_error"]}</div>
        """

    # Build user info tab content
    user_info_tab_html = f"""
        <h2>Authentication</h2>
        <table>
            <tr>
                <td><strong>Username</strong></td>
                <td>{username}</td>
            </tr>
            <tr>
                <td><strong>Authentication Mode</strong></td>
                <td><span class="badge badge-{auth_mode}">{auth_mode}</span></td>
            </tr>
        </table>

        {host_info_html}
        {session_info_html}
        {idp_profile_html}
    """

    # Determine which tabs to show
    show_vector_sync_tab = processing_status is not None
    show_webhooks_tab = is_admin

    # Build vector sync tab content (only if enabled)
    vector_sync_tab_html = ""
    if show_vector_sync_tab:
        vector_sync_tab_html = vector_status_html

    # Build webhooks tab content (only if admin)
    webhooks_tab_html = ""
    if show_webhooks_tab:
        webhooks_tab_html = """
            <div hx-get="/app/webhooks" hx-trigger="load" hx-swap="outerHTML">
                <p style="color: #999;">Loading webhook management...</p>
            </div>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Nextcloud MCP Server</title>

        <!-- htmx for dynamic loading -->
        <script src="https://unpkg.com/htmx.org@1.9.10"></script>

        <!-- Alpine.js for tab state management -->
        <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>

        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                max-width: 900px;
                margin: 50px auto;
                padding: 20px;
                background-color: #f5f5f5;
            }}
            .container {{
                background: white;
                border-radius: 8px;
                padding: 30px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                min-height: calc(100vh - 200px);
            }}
            h1 {{
                color: #0082c9;
                margin-top: 0;
                border-bottom: 2px solid #0082c9;
                padding-bottom: 10px;
            }}
            h2 {{
                color: #333;
                margin-top: 20px;
                border-bottom: 1px solid #e0e0e0;
                padding-bottom: 5px;
            }}

            /* Tab navigation */
            .tabs {{
                display: flex;
                gap: 0;
                margin: 20px 0 0 0;
                border-bottom: 2px solid #e0e0e0;
            }}
            .tab {{
                padding: 12px 24px;
                cursor: pointer;
                background: transparent;
                border: none;
                font-size: 14px;
                font-weight: 500;
                color: #666;
                border-bottom: 2px solid transparent;
                margin-bottom: -2px;
                transition: all 0.2s;
            }}
            .tab:hover {{
                color: #0082c9;
                background-color: #f5f5f5;
            }}
            .tab.active {{
                color: #0082c9;
                border-bottom-color: #0082c9;
            }}

            /* Tab content - use grid to overlay panes */
            .tab-content {{
                padding: 20px 0;
                display: grid;
            }}

            /* Tab panes - all occupy the same grid cell to overlay */
            .tab-pane {{
                grid-area: 1 / 1;
            }}

            /* Tables */
            table {{
                width: 100%;
                border-collapse: collapse;
                margin: 15px 0;
            }}
            td {{
                padding: 10px;
                border-bottom: 1px solid #e0e0e0;
            }}
            td:first-child {{
                width: 200px;
                color: #666;
            }}
            code {{
                background-color: #f5f5f5;
                padding: 2px 6px;
                border-radius: 3px;
                font-family: 'Courier New', monospace;
            }}

            /* Badges */
            .badge {{
                display: inline-block;
                padding: 3px 8px;
                border-radius: 12px;
                font-size: 12px;
                font-weight: bold;
                text-transform: uppercase;
            }}
            .badge-oauth {{
                background-color: #4caf50;
                color: white;
            }}
            .badge-basic {{
                background-color: #2196f3;
                color: white;
            }}

            /* Messages */
            .warning {{
                background-color: #fff3cd;
                border-left: 4px solid #ffc107;
                padding: 15px;
                margin: 15px 0;
                color: #856404;
            }}
            .info-message {{
                background-color: #e3f2fd;
                border-left: 4px solid #2196f3;
                padding: 15px;
                margin: 15px 0;
                color: #1565c0;
            }}

            /* Buttons */
            .button {{
                display: inline-block;
                padding: 10px 20px;
                background-color: #d32f2f;
                color: white;
                text-decoration: none;
                border-radius: 4px;
                transition: background-color 0.3s;
                border: none;
                cursor: pointer;
                font-size: 14px;
            }}
            .button:hover {{
                background-color: #b71c1c;
            }}
            .button-primary {{
                background-color: #0082c9;
            }}
            .button-primary:hover {{
                background-color: #006ba3;
            }}

            /* Logout section */
            .logout {{
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #e0e0e0;
            }}

            /* Smooth htmx content swaps */
            .htmx-swapping {{
                opacity: 0;
                transition: opacity 200ms ease-out;
            }}

            /* Smooth htmx content settling */
            .htmx-settling {{
                opacity: 1;
                transition: opacity 200ms ease-in;
            }}
        </style>
    </head>
    <body>
        <div class="container" x-data="{{ activeTab: 'user-info' }}">
            <h1>Nextcloud MCP Server</h1>

            <!-- Tab Navigation -->
            <div class="tabs">
                <button
                    class="tab"
                    :class="activeTab === 'user-info' ? 'active' : ''"
                    @click="activeTab = 'user-info'">
                    User Info
                </button>
                {
        ""
        if not show_vector_sync_tab
        else '''
                <button
                    class="tab"
                    :class="activeTab === 'vector-sync' ? 'active' : ''"
                    @click="activeTab = 'vector-sync'">
                    Vector Sync
                </button>
                '''
    }
                {
        ""
        if not show_vector_sync_tab
        else '''
                <button
                    class="tab"
                    :class="activeTab === 'vector-viz' ? 'active' : ''"
                    @click="activeTab = 'vector-viz'">
                    Vector Viz
                </button>
                '''
    }
                {
        ""
        if not show_webhooks_tab
        else '''
                <button
                    class="tab"
                    :class="activeTab === 'webhooks' ? 'active' : ''"
                    @click="activeTab = 'webhooks'">
                    Webhooks
                </button>
                '''
    }
            </div>

            <!-- Tab Content -->
            <div class="tab-content">
                <!-- User Info Tab -->
                <div class="tab-pane" x-show="activeTab === 'user-info'" x-transition.opacity.duration.150ms>
                    {user_info_tab_html}
                </div>

                {
        ""
        if not show_vector_sync_tab
        else f'''
                <!-- Vector Sync Tab -->
                <div class="tab-pane" x-show="activeTab === 'vector-sync'" x-transition.opacity.duration.150ms>
                    {vector_sync_tab_html}
                </div>
                '''
    }

                {
        ""
        if not show_vector_sync_tab
        else '''
                <!-- Vector Viz Tab -->
                <div class="tab-pane" x-show="activeTab === 'vector-viz'" x-transition.opacity.duration.150ms>
                    <iframe src="/app/vector-viz" style="width: 100%; height: 800px; border: none;"></iframe>
                </div>
                '''
    }

                {
        ""
        if not show_webhooks_tab
        else f'''
                <!-- Webhooks Tab (admin-only, loaded dynamically) -->
                <div class="tab-pane" x-show="activeTab === 'webhooks'" x-transition.opacity.duration.150ms>
                    {webhooks_tab_html}
                </div>
                '''
    }
            </div>

            {
        f'<div class="logout"><a href="{logout_url}" class="button">Logout</a></div>'
        if auth_mode == "oauth"
        else ""
    }
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)


@requires("authenticated", redirect="oauth_login")
async def revoke_session(request: Request) -> HTMLResponse:
    """Revoke background access (delete refresh token).

    This endpoint allows users to revoke the refresh token that grants
    background access to Nextcloud resources. The session cookie remains
    valid for browser UI access, but background jobs will no longer work.

    Args:
        request: Starlette request object

    Returns:
        HTML response confirming revocation or showing error
    """
    oauth_ctx = getattr(request.app.state, "oauth_context", None)

    if not oauth_ctx:
        return HTMLResponse(
            """
            <!DOCTYPE html>
            <html>
            <head><title>Error</title></head>
            <body>
                <h1>Error</h1>
                <p>OAuth mode not enabled</p>
            </body>
            </html>
            """,
            status_code=400,
        )

    storage = oauth_ctx.get("storage")
    session_id = request.cookies.get("mcp_session")

    if not storage or not session_id:
        return HTMLResponse(
            """
            <!DOCTYPE html>
            <html>
            <head><title>Error</title></head>
            <body>
                <h1>Error</h1>
                <p>Session not found</p>
            </body>
            </html>
            """,
            status_code=400,
        )

    try:
        # Delete the refresh token
        logger.info(f"Revoking background access for session {session_id[:16]}...")
        await storage.delete_refresh_token(session_id)
        logger.info(f"✓ Background access revoked for session {session_id[:16]}...")

        # Redirect back to user page
        user_page_url = str(request.url_for("user_info_html"))

        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta http-equiv="refresh" content="2;url={user_page_url}">
                <title>Background Access Revoked</title>
                <style>
                    body {{
                        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                        max-width: 600px;
                        margin: 50px auto;
                        padding: 20px;
                        text-align: center;
                    }}
                    .success {{
                        background-color: #e8f5e9;
                        border: 2px solid #4caf50;
                        padding: 30px;
                        border-radius: 8px;
                    }}
                    h1 {{
                        color: #4caf50;
                    }}
                </style>
            </head>
            <body>
                <div class="success">
                    <h1>✓ Background Access Revoked</h1>
                    <p>Your refresh token has been deleted successfully.</p>
                    <p>Browser session remains active.</p>
                    <p>Redirecting back to user page...</p>
                </div>
            </body>
            </html>
            """
        )

    except Exception as e:
        logger.error(f"Failed to revoke background access: {e}")
        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
            <head><title>Error</title></head>
            <body>
                <h1>Error</h1>
                <p>Failed to revoke background access: {e}</p>
            </body>
            </html>
            """,
            status_code=500,
        )
