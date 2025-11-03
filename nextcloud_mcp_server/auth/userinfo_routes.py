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

    Args:
        request: Starlette request object (must be authenticated)

    Returns:
        Dictionary containing user information
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

    # OAuth mode - get user's refresh token and current access token
    storage = oauth_ctx.get("storage")
    session_id = request.cookies.get("mcp_session")

    if not storage or not session_id:
        return {
            "error": "Session not found",
            "username": username,
            "auth_mode": "oauth",
        }

    try:
        # Get refresh token data
        token_data = await storage.get_refresh_token(session_id)
        if not token_data:
            return {
                "error": "No refresh token found",
                "username": username,
                "auth_mode": "oauth",
            }

        refresh_token = token_data.get("refresh_token")

        # Exchange refresh token for fresh access token
        oauth_client = oauth_ctx.get("oauth_client")
        oauth_config = oauth_ctx.get("config")

        if oauth_client:
            # External IdP mode (Keycloak)
            # Create fresh HTTP client to avoid event loop issues
            if not oauth_client.token_endpoint:
                await oauth_client.discover()

            async with httpx.AsyncClient(timeout=30.0) as http_client:
                response = await http_client.post(
                    oauth_client.token_endpoint,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    auth=(oauth_client.client_id, oauth_client.client_secret),
                )
                response.raise_for_status()
                token_response = response.json()
                access_token = token_response["access_token"]
        else:
            # Integrated mode (Nextcloud OIDC)
            # Note: This is server-side code, so we use internal Docker hostnames
            # (not public URLs) for server-to-server communication
            discovery_url = oauth_config.get("discovery_url")
            logger.info(f"Querying discovery URL: {discovery_url}")

            async with httpx.AsyncClient() as http_client:
                response = await http_client.get(discovery_url)
                response.raise_for_status()
                discovery = response.json()
                token_endpoint = discovery["token_endpoint"]
                logger.info(
                    f"Using token endpoint for server-side refresh: {token_endpoint}"
                )

            async with httpx.AsyncClient() as http_client:
                response = await http_client.post(
                    token_endpoint,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": oauth_config["client_id"],
                        "client_secret": oauth_config["client_secret"],
                    },
                )
                response.raise_for_status()
                token_response = response.json()
                access_token = token_response["access_token"]

        # Build basic user context
        user_context = {
            "username": username,  # From request.user.display_name
            "auth_mode": "oauth",
            "session_id": session_id[:16] + "...",  # Truncated for security
        }

        # Query IdP userinfo for enhanced profile
        token_verifier = oauth_ctx.get("token_verifier")
        if token_verifier and hasattr(token_verifier, "userinfo_uri"):
            idp_profile = await _query_idp_userinfo(
                access_token, token_verifier.userinfo_uri
            )
            if idp_profile:
                user_context["idp_profile"] = idp_profile
            else:
                user_context["idp_profile_error"] = (
                    "Failed to retrieve profile from IdP"
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
