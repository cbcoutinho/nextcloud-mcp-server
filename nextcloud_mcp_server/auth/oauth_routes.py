"""
OAuth 2.0 Login Routes for ADR-004 Progressive Consent Architecture

Implements dual OAuth flows with explicit provisioning:

Flow 1: Client Authentication - MCP client authenticates directly to IdP
- Client requests: Nextcloud MCP resource scopes (notes:*, calendar:*, etc.)
- Token audience (aud): "mcp-server"
- No server interception - IdP redirects directly to client
- Client receives resource-scoped token for MCP session

Flow 2: Resource Provisioning - MCP server gets delegated Nextcloud access
- Triggered by user calling provision_nextcloud_access tool
- Server requests: openid, profile, email scopes, offline_access
- Separate login flow outside MCP session, results in browser login for user
- Token audience (aud): "nextcloud", redirect/callback to mcp server
- Server receives refresh token for offline access
- Client never sees this token

"""

import logging
import os
from urllib.parse import urlencode

import httpx
import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from nextcloud_mcp_server.auth.client_registry import get_client_registry
from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage

logger = logging.getLogger(__name__)


async def oauth_authorize(request: Request) -> RedirectResponse | JSONResponse:
    """
    OAuth authorization endpoint for Flow 1: Client Authentication.

    The client authenticates directly to the IdP with its own client_id.
    The server validates the client is authorized but does NOT intercept the callback.
    IdP redirects directly back to the client's redirect_uri.

    Query parameters:
        response_type: Must be "code"
        client_id: MCP client identifier (required)
        redirect_uri: Client's localhost redirect URI (required)
        scope: Requested scopes (optional, defaults to "openid profile email")
        state: CSRF protection state (required)
        code_challenge: PKCE code challenge from client (required)
        code_challenge_method: PKCE method, must be "S256" (required)

    Returns:
        302 redirect to IdP authorization endpoint
    """
    # Extract parameters
    response_type = request.query_params.get("response_type")
    client_id = request.query_params.get("client_id")
    redirect_uri = request.query_params.get("redirect_uri")
    state = request.query_params.get("state")
    code_challenge = request.query_params.get("code_challenge")
    code_challenge_method = request.query_params.get("code_challenge_method", "S256")

    # Validate required parameters
    if response_type != "code":
        return JSONResponse(
            {
                "error": "unsupported_response_type",
                "error_description": "Only 'code' response_type is supported",
            },
            status_code=400,
        )

    if not redirect_uri:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "redirect_uri is required",
            },
            status_code=400,
        )

    # Validate redirect_uri is localhost (RFC 8252 for native clients)
    if not redirect_uri.startswith(("http://localhost:", "http://127.0.0.1:")):
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "redirect_uri must be localhost for native clients",
            },
            status_code=400,
        )

    if not state:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "state parameter is required for CSRF protection",
            },
            status_code=400,
        )

    if not code_challenge:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "code_challenge is required (PKCE)",
            },
            status_code=400,
        )

    if code_challenge_method != "S256":
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "code_challenge_method must be S256",
            },
            status_code=400,
        )

    # Validate client_id (required for Progressive Consent Flow 1)
    if not client_id:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "client_id is required",
            },
            status_code=400,
        )

    # Validate client using registry
    registry = get_client_registry()
    is_valid, error_msg = registry.validate_client(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=request.query_params.get("scope", "").split()
        if request.query_params.get("scope")
        else None,
    )

    if not is_valid:
        logger.warning(f"Client validation failed: {error_msg}")
        return JSONResponse(
            {
                "error": "unauthorized_client",
                "error_description": error_msg,
            },
            status_code=401,
        )

    # Get OAuth context from app state
    oauth_ctx = request.app.state.oauth_context
    if not oauth_ctx:
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "OAuth not configured on server",
            },
            status_code=500,
        )

    oauth_client = oauth_ctx["oauth_client"]
    oauth_config = oauth_ctx["config"]

    # Flow 1: Client authenticates directly to IdP WITHOUT server interception
    # CRITICAL: This is a direct pass-through to IdP
    # The IdP will redirect directly back to the client's callback
    # The MCP server does NOT see the IdP authorization code!

    logger.info(
        f"Starting Progressive Consent Flow 1 - no server session needed, "
        f"client will handle IdP response directly at {redirect_uri}"
    )

    # Use client's redirect_uri for DIRECT callback (bypasses server)
    callback_uri = redirect_uri

    # Request resource scopes for MCP tools access
    # The token will have aud: "mcp-server" claim
    # Build scopes from NEXTCLOUD_OIDC_SCOPES config
    default_scopes = "openid profile email"
    resource_scopes = oauth_config.get("scopes", "")
    scopes = f"{default_scopes} {resource_scopes}".strip()

    # Pass through client's state directly
    idp_state = state

    # Use client's own client_id (client must be pre-registered at IdP)
    idp_client_id = client_id

    logger.info("Flow 1 (Progressive Consent): Direct client auth to IdP")
    logger.info(f"  Client ID: {client_id}")
    logger.info(f"  Client will receive IdP code directly at: {callback_uri}")
    logger.info(f"  Scopes: {scopes} (resource access for MCP tools)")

    # Get authorization endpoint from OAuth client
    if oauth_client:
        # External IdP mode (Keycloak) - use oauth_client
        auth_url = await oauth_client.get_authorization_url(
            state=idp_state,
            code_challenge="",  # Server doesn't use PKCE with IdP
        )
        logger.info(f"Redirecting to external IdP: {auth_url.split('?')[0]}")
    else:
        # Integrated mode (Nextcloud OIDC) - build URL directly
        discovery_url = oauth_config.get("discovery_url")
        if not discovery_url:
            return JSONResponse(
                {
                    "error": "server_error",
                    "error_description": "OAuth discovery URL not configured",
                },
                status_code=500,
            )

        # Fetch authorization endpoint from discovery
        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(discovery_url)
            response.raise_for_status()
            discovery = response.json()
            authorization_endpoint = discovery["authorization_endpoint"]

        # IMPORTANT: Replace internal Docker hostname with public URL for browser access
        # The discovery endpoint returns http://app/apps/oidc/authorize (internal)
        # But browsers need http://localhost:8080/apps/oidc/authorize (public)
        from urllib.parse import urlparse as parse_url

        public_issuer = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL")
        if public_issuer:
            # Parse internal and authorization endpoint to compare hostnames
            internal_parsed = parse_url(oauth_config["nextcloud_host"])
            auth_parsed = parse_url(authorization_endpoint)

            # Check if authorization endpoint uses internal hostname
            if auth_parsed.hostname == internal_parsed.hostname:
                # Replace internal hostname+port with public URL
                # Keep the path from authorization_endpoint
                public_parsed = parse_url(public_issuer)
                authorization_endpoint = (
                    f"{public_parsed.scheme}://{public_parsed.netloc}{auth_parsed.path}"
                )
                if auth_parsed.query:
                    authorization_endpoint += f"?{auth_parsed.query}"
                logger.info(
                    f"Rewrote authorization endpoint for browser access: {authorization_endpoint}"
                )

        idp_params = {
            "client_id": idp_client_id,
            "redirect_uri": callback_uri,
            "response_type": "code",
            "scope": scopes,
            "state": idp_state,
            "prompt": "consent",  # Ensure refresh token
            "resource": f"{oauth_config['mcp_server_url']}/mcp",  # MCP server audience
        }

        auth_url = f"{authorization_endpoint}?{urlencode(idp_params)}"
        logger.info(f"Redirecting to Nextcloud OIDC: {auth_url.split('?')[0]}")

    return RedirectResponse(auth_url, status_code=302)


async def oauth_authorize_nextcloud(
    request: Request,
) -> RedirectResponse | JSONResponse:
    """
    OAuth authorization endpoint for Flow 2: Resource Provisioning.

    This endpoint is used by the provision_nextcloud_access MCP tool
    to initiate delegated resource access to Nextcloud. Requires a separate
    login flow outside of the MCP session.

    Query parameters:
        state: Session state for tracking

    Returns:
        302 redirect to IdP authorization endpoint
    """
    state = request.query_params.get("state")
    if not state:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "state parameter is required",
            },
            status_code=400,
        )

    # Get OAuth context
    oauth_ctx = request.app.state.oauth_context
    if not oauth_ctx:
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "OAuth not configured on server",
            },
            status_code=500,
        )

    oauth_config = oauth_ctx["config"]

    # Get MCP server's OAuth client credentials
    mcp_server_client_id = os.getenv(
        "MCP_SERVER_CLIENT_ID", oauth_config.get("client_id")
    )
    if not mcp_server_client_id:
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "MCP server OAuth client not configured",
            },
            status_code=500,
        )

    mcp_server_url = oauth_config["mcp_server_url"]
    callback_uri = f"{mcp_server_url}/oauth/callback-nextcloud"

    # Flow 2: Server only needs identity + offline access (no resource scopes)
    # Resource scopes are requested by client in Flow 1
    scopes = "openid profile email offline_access"

    # Get authorization endpoint
    discovery_url = oauth_config.get("discovery_url")
    if not discovery_url:
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "OAuth discovery URL not configured",
            },
            status_code=500,
        )

    async with httpx.AsyncClient() as http_client:
        response = await http_client.get(discovery_url)
        response.raise_for_status()
        discovery = response.json()
        authorization_endpoint = discovery["authorization_endpoint"]

    # Fix internal hostname for browser access
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

    # Build authorization URL
    idp_params = {
        "client_id": mcp_server_client_id,
        "redirect_uri": callback_uri,
        "response_type": "code",
        "scope": scopes,
        "state": state,
        "prompt": "consent",  # Force consent to show resource access
        "access_type": "offline",  # Request refresh token
        "resource": oauth_config["nextcloud_resource_uri"],  # Nextcloud audience
    }

    auth_url = f"{authorization_endpoint}?{urlencode(idp_params)}"
    logger.info("Flow 2: Redirecting to IdP for resource provisioning")

    return RedirectResponse(auth_url, status_code=302)


async def oauth_callback_nextcloud(request: Request):
    """
    OAuth callback endpoint for Flow 2: Resource Provisioning.

    The IdP redirects here after user grants delegated resource access.
    Server stores the master refresh token for offline access.

    Query parameters:
        code: Authorization code from IdP
        state: State parameter (session identifier)
        error: Error code (if authorization failed)

    Returns:
        JSON response or HTML success page
    """
    # Check for errors from IdP
    error = request.query_params.get("error")
    if error:
        error_description = request.query_params.get(
            "error_description", "Authorization failed"
        )
        logger.error(f"Flow 2 authorization error: {error} - {error_description}")
        return JSONResponse(
            {
                "error": error,
                "error_description": error_description,
            },
            status_code=400,
        )

    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or not state:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "code and state parameters are required",
            },
            status_code=400,
        )

    # Get OAuth context
    oauth_ctx = request.app.state.oauth_context
    storage: RefreshTokenStorage = oauth_ctx["storage"]
    oauth_config = oauth_ctx["config"]

    # Exchange code for tokens
    mcp_server_client_id = os.getenv(
        "MCP_SERVER_CLIENT_ID", oauth_config.get("client_id")
    )
    mcp_server_client_secret = os.getenv(
        "MCP_SERVER_CLIENT_SECRET", oauth_config.get("client_secret")
    )
    mcp_server_url = oauth_config["mcp_server_url"]
    callback_uri = f"{mcp_server_url}/oauth/callback-nextcloud"

    discovery_url = oauth_config.get("discovery_url")
    async with httpx.AsyncClient() as http_client:
        response = await http_client.get(discovery_url)
        response.raise_for_status()
        discovery = response.json()
        token_endpoint = discovery["token_endpoint"]

    # Exchange code for tokens
    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_uri,
                "client_id": mcp_server_client_id,
                "client_secret": mcp_server_client_secret,
            },
        )
        response.raise_for_status()
        token_data = response.json()

    refresh_token = token_data.get("refresh_token")
    id_token = token_data.get("id_token")

    # Decode ID token to get user info
    try:
        userinfo = jwt.decode(id_token, options={"verify_signature": False})
        user_id = userinfo.get("sub")
        username = userinfo.get("preferred_username") or userinfo.get("email")
        logger.info(f"Flow 2: User {username} provisioned resource access")
    except Exception as e:
        logger.warning(f"Failed to decode ID token: {e}")
        user_id = "unknown"

    # Store master refresh token for Flow 2
    if refresh_token:
        # Parse granted scopes from token response
        granted_scopes = (
            token_data.get("scope", "").split() if token_data.get("scope") else None
        )

        await storage.store_refresh_token(
            user_id=user_id,
            refresh_token=refresh_token,
            flow_type="flow2",
            token_audience="nextcloud",
            provisioning_client_id=state,  # Store which client initiated provisioning
            scopes=granted_scopes,
            expires_at=None,  # Refresh tokens typically don't expire
        )
        logger.info(f"Stored Flow 2 master refresh token for user {user_id}")

    # Return success HTML page
    success_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Nextcloud Access Provisioned</title>
        <style>
            body { font-family: Arial, sans-serif; text-align: center; margin-top: 50px; }
            .success { color: green; }
            .info { margin-top: 20px; color: #666; }
        </style>
    </head>
    <body>
        <h1 class="success">✓ Nextcloud Access Provisioned</h1>
        <p>The MCP server now has offline access to your Nextcloud resources.</p>
        <p class="info">You can close this window and return to your MCP client.</p>
    </body>
    </html>
    """

    from starlette.responses import HTMLResponse

    return HTMLResponse(content=success_html, status_code=200)
