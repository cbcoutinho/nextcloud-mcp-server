"""Browser-based OAuth login routes for admin UI.

Separate from MCP OAuth flow - these routes establish browser sessions
for accessing admin UI endpoints like /user/page.
"""

import logging
import os
import secrets
from urllib.parse import urlencode

import httpx
import jwt
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from nextcloud_mcp_server.auth.userinfo_routes import (
    _get_userinfo_endpoint,
    _query_idp_userinfo,
)

logger = logging.getLogger(__name__)


async def oauth_login(request: Request) -> RedirectResponse | JSONResponse:
    """Browser OAuth login endpoint - redirects to IdP for authentication.

    This is separate from the MCP OAuth flow (/oauth/authorize).
    Creates a browser session with refresh token for admin UI access.

    Query parameters:
        next: Optional URL to redirect to after login (default: /user/page)

    Returns:
        302 redirect to IdP authorization endpoint
    """
    oauth_ctx = request.app.state.oauth_context
    if not oauth_ctx:
        # BasicAuth mode - no login needed, redirect to user page
        return RedirectResponse("/user/page", status_code=302)

    storage = oauth_ctx["storage"]
    oauth_client = oauth_ctx["oauth_client"]
    oauth_config = oauth_ctx["config"]

    # Debug: Log oauth_config contents
    logger.info(f"oauth_login called - oauth_config keys: {oauth_config.keys()}")
    logger.info(f"oauth_login called - client_id: {oauth_config.get('client_id')}")
    logger.info(f"oauth_login called - oauth_client: {oauth_client is not None}")

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)

    # Build OAuth authorization URL
    mcp_server_url = oauth_config["mcp_server_url"]
    callback_uri = f"{mcp_server_url}/oauth/login-callback"

    # Request only basic OIDC scopes for browser session
    # Note: Nextcloud app scopes (notes:read, etc.) are for MCP client access tokens,
    # not for the MCP server's own browser authentication
    scopes = "openid profile email offline_access"

    code_challenge = ""
    code_verifier = ""

    if oauth_client:
        # External IdP mode (Keycloak)
        # Keycloak requires PKCE, so generate code_verifier and code_challenge
        if not oauth_client.authorization_endpoint:
            await oauth_client.discover()

        # Generate PKCE values
        code_verifier, code_challenge = oauth_client.generate_pkce_challenge()

        # Store code_verifier temporarily (using state as key)
        # We'll retrieve it in the callback using the state parameter
        await storage.store_oauth_session(
            session_id=state,  # Use state as session ID
            client_id="browser-ui",
            client_redirect_uri="/user/page",
            state=state,
            code_challenge=code_challenge,
            code_challenge_method="S256",
            mcp_authorization_code=code_verifier,  # Store code_verifier here temporarily
            flow_type="browser",
            ttl_seconds=600,  # 10 minutes
        )

        idp_params = {
            "client_id": oauth_client.client_id,
            "redirect_uri": callback_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "consent",  # Ensure refresh token
        }

        auth_url = f"{oauth_client.authorization_endpoint}?{urlencode(idp_params)}"
        logger.info(f"Redirecting to external IdP login: {auth_url.split('?')[0]}")
    else:
        # Integrated mode (Nextcloud OIDC)
        discovery_url = oauth_config.get("discovery_url")
        if not discovery_url:
            return JSONResponse(
                {
                    "error": "server_error",
                    "error_description": "OAuth discovery URL not configured",
                },
                status_code=500,
            )

        # Fetch authorization endpoint
        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(discovery_url)
            response.raise_for_status()
            discovery = response.json()
            authorization_endpoint = discovery["authorization_endpoint"]

        # Replace internal Docker hostname with public URL
        public_issuer = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL")
        if public_issuer:
            from urllib.parse import urlparse as parse_url

            internal_parsed = parse_url(oauth_config["nextcloud_host"])
            auth_parsed = parse_url(authorization_endpoint)

            if auth_parsed.hostname == internal_parsed.hostname:
                public_parsed = parse_url(public_issuer)
                authorization_endpoint = (
                    f"{public_parsed.scheme}://{public_parsed.netloc}{auth_parsed.path}"
                )

        idp_params = {
            "client_id": oauth_config["client_id"],
            "redirect_uri": callback_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
            "prompt": "consent",  # Ensure refresh token
        }

        # Debug: Log full parameters
        logger.info(f"Building Nextcloud OIDC auth URL with params: {idp_params}")

        auth_url = f"{authorization_endpoint}?{urlencode(idp_params)}"
        logger.info(f"Redirecting to Nextcloud OIDC login: {auth_url}")

    return RedirectResponse(auth_url, status_code=302)


async def oauth_login_callback(request: Request) -> RedirectResponse | HTMLResponse:
    """Browser OAuth callback - IdP redirects here after authentication.

    Exchanges authorization code for tokens, stores refresh token,
    sets session cookie, and redirects to original destination.

    Query parameters:
        code: Authorization code from IdP
        state: State parameter
        error: Error code (if authorization failed)

    Returns:
        302 redirect to next URL with session cookie
    """
    # Check for errors
    error = request.query_params.get("error")
    if error:
        error_description = request.query_params.get(
            "error_description", "Authorization failed"
        )
        logger.error(f"OAuth login error: {error} - {error_description}")
        login_url = str(request.url_for("oauth_login"))
        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
            <head><title>Login Failed</title></head>
            <body>
                <h1>Login Failed</h1>
                <p>Error: {error}</p>
                <p>{error_description}</p>
                <p><a href="{login_url}">Try again</a></p>
            </body>
            </html>
            """,
            status_code=400,
        )

    # Extract code and state
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or not state:
        return HTMLResponse(
            """
            <!DOCTYPE html>
            <html>
            <head><title>Invalid Request</title></head>
            <body>
                <h1>Invalid Request</h1>
                <p>Missing code or state parameter</p>
            </body>
            </html>
            """,
            status_code=400,
        )

    # Get OAuth context
    oauth_ctx = request.app.state.oauth_context
    storage = oauth_ctx["storage"]
    oauth_client = oauth_ctx["oauth_client"]
    oauth_config = oauth_ctx["config"]

    # Retrieve code_verifier from session storage (if using PKCE)
    code_verifier = ""
    if oauth_client:
        # For Keycloak (external IdP), we stored the code_verifier in the session
        oauth_session = await storage.get_oauth_session(state)
        if oauth_session:
            # code_verifier was stored in mcp_authorization_code field
            code_verifier = oauth_session.get("mcp_authorization_code", "")
            # Clean up the temporary session
            # Note: We don't have delete_oauth_session method, but it will expire after TTL

    # Exchange authorization code for tokens
    mcp_server_url = oauth_config["mcp_server_url"]
    callback_uri = f"{mcp_server_url}/oauth/login-callback"

    try:
        if oauth_client:
            # External IdP mode (Keycloak)
            # Use PKCE if we have a code_verifier
            if not oauth_client.token_endpoint:
                await oauth_client.discover()

            token_params = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_uri,
                "client_id": oauth_client.client_id,
                "client_secret": oauth_client.client_secret,
            }

            # Add code_verifier if we have one (PKCE)
            if code_verifier:
                token_params["code_verifier"] = code_verifier

            async with httpx.AsyncClient() as http_client:
                response = await http_client.post(
                    oauth_client.token_endpoint,
                    data=token_params,
                )
                response.raise_for_status()
                token_data = response.json()
        else:
            # Integrated mode (Nextcloud OIDC)
            discovery_url = oauth_config.get("discovery_url")
            async with httpx.AsyncClient() as http_client:
                response = await http_client.get(discovery_url)
                response.raise_for_status()
                discovery = response.json()
                token_endpoint = discovery["token_endpoint"]

            async with httpx.AsyncClient() as http_client:
                response = await http_client.post(
                    token_endpoint,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": callback_uri,
                        "client_id": oauth_config["client_id"],
                        "client_secret": oauth_config["client_secret"],
                    },
                )
                response.raise_for_status()
                token_data = response.json()

    except httpx.HTTPStatusError as e:
        error_body = (
            e.response.text if hasattr(e.response, "text") else str(e.response.content)
        )
        logger.error(
            f"Token exchange failed: HTTP {e.response.status_code} - {error_body}"
        )
        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
            <head><title>Login Failed</title></head>
            <body>
                <h1>Login Failed</h1>
                <p>Failed to exchange authorization code for tokens</p>
                <p>HTTP {e.response.status_code}: {error_body}</p>
            </body>
            </html>
            """,
            status_code=500,
        )
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
            <head><title>Login Failed</title></head>
            <body>
                <h1>Login Failed</h1>
                <p>Failed to exchange authorization code for tokens</p>
                <p>Error: {e}</p>
            </body>
            </html>
            """,
            status_code=500,
        )

    refresh_token = token_data.get("refresh_token")
    id_token = token_data.get("id_token")

    logger.info(f"Token exchange response keys: {token_data.keys()}")
    logger.info(f"Refresh token present: {refresh_token is not None}")
    logger.info(f"ID token present: {id_token is not None}")

    # Decode ID token to get user info
    try:
        userinfo = jwt.decode(id_token, options={"verify_signature": False})
        user_id = userinfo.get("sub")
        username = userinfo.get("preferred_username") or userinfo.get("email")
        logger.info(f"Browser login successful: {username} (sub={user_id})")
    except Exception as e:
        logger.warning(f"Failed to decode ID token: {e}")
        user_id = f"user-{secrets.token_hex(8)}"
        username = "unknown"

    # Store refresh token (for background jobs ONLY)
    if refresh_token:
        logger.info(f"Storing refresh token for user_id: {user_id}")
        await storage.store_refresh_token(
            user_id=user_id,
            refresh_token=refresh_token,
            expires_at=None,
            flow_type="browser",  # Browser-based login flow
        )
        logger.info(f"✓ Refresh token stored successfully for user_id: {user_id}")
    else:
        logger.warning("No refresh token in token response - cannot store session")

    # Query and cache user profile (for browser UI display)
    access_token = token_data.get("access_token")
    if access_token:
        try:
            # Get the OAuth context to determine correct userinfo endpoint
            oauth_ctx = getattr(request.app.state, "oauth_context", {})
            userinfo_endpoint = await _get_userinfo_endpoint(oauth_ctx)

            if userinfo_endpoint:
                # Query userinfo endpoint with fresh access token
                profile_data = await _query_idp_userinfo(
                    access_token, userinfo_endpoint
                )

                if profile_data:
                    # Cache profile for browser UI (no token needed to display)
                    await storage.store_user_profile(user_id, profile_data)
                    logger.info(f"✓ User profile cached for {user_id}")
                else:
                    logger.warning(f"Failed to query userinfo endpoint for {user_id}")
            else:
                logger.warning("Could not determine userinfo endpoint")
        except Exception as e:
            logger.error(f"Error caching user profile: {e}")
            # Continue anyway - profile cache is optional for browser UI

    # Create response and set session cookie
    response = RedirectResponse("/user/page", status_code=302)
    response.set_cookie(
        key="mcp_session",
        value=user_id,
        max_age=86400 * 30,  # 30 days
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",
    )

    logger.info(f"Session cookie set for user: {username}")
    return response


async def oauth_logout(request: Request) -> RedirectResponse:
    """Browser OAuth logout - clears session cookie.

    Query parameters:
        next: Optional URL to redirect to after logout (default: /oauth/login)

    Returns:
        302 redirect with cleared session cookie
    """
    next_url = request.query_params.get("next", "/oauth/login")

    # TODO: Optionally revoke refresh token from storage
    # session_id = request.cookies.get("mcp_session")
    # if session_id:
    #     await storage.delete_refresh_token(session_id)

    response = RedirectResponse(next_url, status_code=302)
    response.delete_cookie("mcp_session")

    logger.info("User logged out, session cookie cleared")
    return response
