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
        token_data = await storage.get_refresh_token(session_id)
        background_access_granted = token_data is not None

        # Retrieve cached user profile (no token operations!)
        profile_data = await storage.get_user_profile(session_id)

        # Build user context
        user_context = {
            "username": username,  # From request.user.display_name (session_id)
            "auth_mode": "oauth",
            "session_id": session_id[:16] + "...",  # Truncated for security
            "background_access_granted": background_access_granted,
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
        session_info_html = f"""
        <h2>Session Information</h2>
        <table>
            <tr>
                <td><strong>Session ID</strong></td>
                <td><code>{session_id}</code></td>
            </tr>
        </table>
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

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>User Info - Nextcloud MCP Server</title>
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
                color: #0082c9;
                margin-top: 0;
                border-bottom: 2px solid #0082c9;
                padding-bottom: 10px;
            }}
            h2 {{
                color: #333;
                margin-top: 30px;
                border-bottom: 1px solid #e0e0e0;
                padding-bottom: 5px;
            }}
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
            .warning {{
                background-color: #fff3cd;
                border-left: 4px solid #ffc107;
                padding: 15px;
                margin: 15px 0;
                color: #856404;
            }}
            .logout {{
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #e0e0e0;
            }}
            .button {{
                display: inline-block;
                padding: 10px 20px;
                background-color: #d32f2f;
                color: white;
                text-decoration: none;
                border-radius: 4px;
                transition: background-color 0.3s;
            }}
            .button:hover {{
                background-color: #b71c1c;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Nextcloud MCP Server - User Info</h1>

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

            {f'<div class="logout"><a href="{logout_url}" class="button">Logout</a></div>' if auth_mode == "oauth" else ""}
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)
