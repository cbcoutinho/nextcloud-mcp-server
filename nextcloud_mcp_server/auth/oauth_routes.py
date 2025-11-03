"""
OAuth 2.0 Login Routes for ADR-004 Progressive Consent Architecture

Implements OAuth endpoints that support both:
1. Hybrid Flow (default, backward compatible) - Single OAuth flow with server interception
2. Progressive Consent (opt-in via ENABLE_PROGRESSIVE_CONSENT=true) - Dual OAuth flows with explicit provisioning

Progressive Consent Mode (opt-in, requires separate login):
- Enable with ENABLE_PROGRESSIVE_CONSENT=true
- Flow 1: Client Authentication - MCP client authenticates directly to IdP
- Flow 2: Resource Provisioning - MCP server gets delegated Nextcloud access (separate login, not during MCP session)

Hybrid Flow Mode (default, backward compatible):
1. MCP client initiates OAuth at /oauth/authorize
2. MCP server redirects to IdP (intercepts callback)
3. IdP redirects back to /oauth/callback (server gets master tokens)
4. Server generates MCP auth code and redirects to client
5. Client exchanges MCP code at /oauth/token using PKCE
"""

import hashlib
import logging
import os
import secrets
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from nextcloud_mcp_server.auth.client_registry import get_client_registry
from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage

logger = logging.getLogger(__name__)


async def oauth_authorize(request: Request) -> RedirectResponse | JSONResponse:
    """
    OAuth authorization endpoint with PKCE support.

    Supports both Hybrid Flow (default) and Progressive Consent Flow 1 (opt-in).

    In Progressive Consent mode (opt-in, ENABLE_PROGRESSIVE_CONSENT=true):
    - Flow 1: Client authenticates directly to IdP with its own client_id
    - Server validates client_id is in ALLOWED_MCP_CLIENTS list
    - Issues tokens with aud: "mcp-server" for MCP authentication only

    In Hybrid Flow mode (default):
    - Single OAuth flow where server intercepts and stores refresh token

    Query parameters:
        response_type: Must be "code"
        client_id: MCP client identifier (required in Progressive mode)
        redirect_uri: Client's localhost redirect URI (required)
        scope: Requested scopes (optional)
        state: CSRF protection state (required)
        code_challenge: PKCE code challenge from client (required)
        code_challenge_method: PKCE method, must be "S256" (required)

    Returns:
        302 redirect to IdP authorization endpoint
    """
    # Check if Progressive Consent is enabled (opt-in, defaults to false)
    enable_progressive = (
        os.getenv("ENABLE_PROGRESSIVE_CONSENT", "false").lower() == "true"
    )

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

    # In Progressive Consent mode, validate client_id using registry
    if enable_progressive:
        if not client_id:
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "error_description": "client_id is required in Progressive Consent mode",
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

    storage: RefreshTokenStorage = oauth_ctx["storage"]
    oauth_client = oauth_ctx["oauth_client"]
    oauth_config = oauth_ctx["config"]

    # Build IdP authorization URL
    mcp_server_url = oauth_config["mcp_server_url"]

    if enable_progressive:
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

        # Only request MCP authentication scopes (no Nextcloud scopes!)
        # The token will have aud: "mcp-server" claim
        scopes = "openid profile email"

        # Pass through client's state directly
        idp_state = state

        # Use client's own client_id (client must be pre-registered at IdP)
        idp_client_id = client_id

        logger.info("Flow 1 (Progressive Consent): Direct client auth to IdP")
        logger.info(f"  Client ID: {client_id}")
        logger.info(f"  Client will receive IdP code directly at: {callback_uri}")
        logger.info(f"  Scopes: {scopes} (no resource access)")
    else:
        # Hybrid Flow: Server intercepts callback (backward compatible)
        # Generate session ID and MCP authorization code for Hybrid Flow
        session_id = str(uuid4())
        mcp_authorization_code = f"mcp-code-{secrets.token_urlsafe(32)}"

        logger.info(
            f"Starting Hybrid OAuth flow - session={session_id[:8]}..., "
            f"client_redirect={redirect_uri}"
        )

        # Store session with client details and PKCE challenge
        await storage.store_oauth_session(
            session_id=session_id,
            client_id=client_id,
            client_redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            mcp_authorization_code=mcp_authorization_code,
            flow_type="hybrid",
            ttl_seconds=600,  # 10 minutes
        )

        callback_uri = f"{mcp_server_url}/oauth/callback"
        # Combine session_id and client state for IdP state parameter
        idp_state = f"{session_id}:{state}"
        # Build scopes - include both identity scopes and Nextcloud scopes
        default_scopes = "openid profile email offline_access"
        nextcloud_scopes = oauth_config.get("scopes", "")
        scopes = f"{default_scopes} {nextcloud_scopes}".strip()
        # Use server's client_id
        idp_client_id = oauth_config["client_id"]

        logger.info("Hybrid Flow: Server intercepts callback")
        logger.info(f"  Server callback: {callback_uri}")
        logger.info(f"  Combined scopes: {scopes}")

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
        }

        auth_url = f"{authorization_endpoint}?{urlencode(idp_params)}"
        logger.info(f"Redirecting to Nextcloud OIDC: {auth_url.split('?')[0]}")

    return RedirectResponse(auth_url, status_code=302)


async def oauth_callback(request: Request) -> RedirectResponse | JSONResponse:
    """
    OAuth callback endpoint - IdP redirects here after user authentication.

    This is the CRITICAL difference in the Hybrid Flow:
    - The server receives the IdP authorization code
    - Server exchanges it for master tokens (including refresh token)
    - Server stores the refresh token securely
    - Server generates MCP authorization code
    - Server redirects client with MCP code (not IdP code!)

    Query parameters:
        code: Authorization code from IdP
        state: State parameter (contains session_id:client_state)
        error: Error code (if authorization failed)
        error_description: Error description

    Returns:
        302 redirect to client's redirect_uri with MCP authorization code
    """
    # Check for errors from IdP
    error = request.query_params.get("error")
    if error:
        error_description = request.query_params.get(
            "error_description", "Authorization failed"
        )
        logger.error(f"IdP authorization error: {error} - {error_description}")
        return JSONResponse(
            {
                "error": error,
                "error_description": error_description,
            },
            status_code=400,
        )

    # Extract IdP authorization code and state
    idp_code = request.query_params.get("code")
    idp_state = request.query_params.get("state")

    if not idp_code or not idp_state:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "code and state parameters are required",
            },
            status_code=400,
        )

    # Parse state to extract session_id and client_state
    try:
        session_id, client_state = idp_state.split(":", 1)
    except ValueError:
        return JSONResponse(
            {"error": "invalid_state", "error_description": "Invalid state format"},
            status_code=400,
        )

    # Get OAuth context
    oauth_ctx = request.app.state.oauth_context
    storage: RefreshTokenStorage = oauth_ctx["storage"]
    oauth_client = oauth_ctx["oauth_client"]
    oauth_config = oauth_ctx["config"]

    # Retrieve OAuth session
    oauth_session = await storage.get_oauth_session(session_id)
    if not oauth_session:
        return JSONResponse(
            {
                "error": "invalid_session",
                "error_description": "Session not found or expired",
            },
            status_code=400,
        )

    logger.info(
        f"Processing OAuth callback - session={session_id[:8]}..., "
        f"exchanging IdP code for tokens"
    )

    # STEP 1: Exchange IdP code for master tokens
    # The server gets the master refresh token!
    mcp_server_url = oauth_config["mcp_server_url"]
    server_callback_uri = f"{mcp_server_url}/oauth/callback"

    try:
        if oauth_client:
            # External IdP mode (Keycloak)
            # Note: This requires code_verifier, but server doesn't use PKCE with IdP
            # We'll need to modify KeycloakOAuthClient to support this pattern
            token_data = await oauth_client.exchange_authorization_code(
                code=idp_code,
                code_verifier="",  # Server doesn't use PKCE with IdP
            )
        else:
            # Integrated mode (Nextcloud OIDC)
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
                        "code": idp_code,
                        "redirect_uri": server_callback_uri,
                        "client_id": oauth_config["client_id"],
                        "client_secret": oauth_config["client_secret"],
                    },
                )
                response.raise_for_status()
                token_data = response.json()

    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": f"Failed to exchange authorization code: {e}",
            },
            status_code=500,
        )

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    id_token = token_data.get("id_token")

    # Decode ID token to get user info (without verification - just for userinfo)
    try:
        userinfo = jwt.decode(id_token, options={"verify_signature": False})
        user_id = userinfo.get("sub")
        username = userinfo.get("preferred_username") or userinfo.get("email")

        logger.info(f"User authenticated: {username} (sub={user_id})")

    except Exception as e:
        logger.warning(f"Failed to decode ID token: {e}")
        user_id = "unknown"
        username = "unknown"

    # STEP 2: Store master refresh token (if provided)
    if refresh_token:
        await storage.store_refresh_token(
            user_id=user_id,
            refresh_token=refresh_token,
            expires_at=None,  # Refresh tokens typically don't have expiration
        )
        logger.info(f"Stored master refresh token for user {user_id}")

    # STEP 3: Update session with tokens
    await storage.update_oauth_session(
        session_id=session_id,
        user_id=user_id,
        idp_access_token=access_token,
        idp_refresh_token=refresh_token,
    )

    # STEP 4: Redirect to native client with MCP-generated code
    mcp_code = oauth_session["mcp_authorization_code"]
    client_redirect_uri = oauth_session["client_redirect_uri"]

    redirect_params = {
        "code": mcp_code,  # MCP code, NOT IdP code!
        "state": client_state,  # Return original client state
    }

    redirect_url = f"{client_redirect_uri}?{urlencode(redirect_params)}"

    logger.info(
        f"OAuth callback complete - redirecting to client with MCP code: {mcp_code[:16]}..."
    )

    return RedirectResponse(redirect_url, status_code=302)


async def oauth_token(request: Request) -> JSONResponse:
    """
    OAuth token endpoint - client exchanges MCP code for tokens.

    The client sends the MCP-generated code (not IdP code) and proves
    ownership via PKCE code_verifier.

    Form parameters:
        grant_type: Must be "authorization_code" or "refresh_token"
        code: MCP authorization code (for authorization_code grant)
        code_verifier: PKCE code verifier (for authorization_code grant)
        redirect_uri: Must match the redirect_uri from /oauth/authorize
        client_id: MCP client identifier (optional)
        refresh_token: Refresh token (for refresh_token grant)

    Returns:
        JSON response with access_token and optional refresh_token
    """
    # Parse form data
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        # Authorization code grant
        code = form.get("code")
        code_verifier = form.get("code_verifier")
        redirect_uri = form.get("redirect_uri")

        if not code or not code_verifier or not redirect_uri:
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "error_description": "code, code_verifier, and redirect_uri are required",
                },
                status_code=400,
            )

        # Get OAuth context
        oauth_ctx = request.app.state.oauth_context
        storage: RefreshTokenStorage = oauth_ctx["storage"]

        # Retrieve session by MCP authorization code
        oauth_session = await storage.get_oauth_session_by_mcp_code(code)
        if not oauth_session:
            return JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "Invalid authorization code",
                },
                status_code=400,
            )

        # Verify PKCE
        code_challenge = oauth_session.get("code_challenge")
        if code_challenge:
            # Compute challenge from verifier
            computed_challenge = hashlib.sha256(code_verifier.encode()).digest().hex()
            # Convert to base64url format
            import base64

            computed_challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(code_verifier.encode()).digest()
                )
                .decode()
                .rstrip("=")
            )

            if computed_challenge != code_challenge:
                logger.error("PKCE verification failed")
                return JSONResponse(
                    {
                        "error": "invalid_grant",
                        "error_description": "PKCE verification failed",
                    },
                    status_code=400,
                )

        # Verify redirect_uri matches
        if redirect_uri != oauth_session["client_redirect_uri"]:
            return JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "redirect_uri mismatch",
                },
                status_code=400,
            )

        # Get stored IdP access token
        idp_access_token = oauth_session.get("idp_access_token")
        if not idp_access_token:
            return JSONResponse(
                {
                    "error": "server_error",
                    "error_description": "Access token not found in session",
                },
                status_code=500,
            )

        # Invalidate MCP authorization code (one-time use)
        await storage.delete_oauth_session(oauth_session["session_id"])

        logger.info(f"Token exchange successful - user={oauth_session.get('user_id')}")

        # Return tokens to client
        # CRITICAL: Client gets access token but NOT the master refresh token
        # (unless we implement MCP session refresh tokens)
        return JSONResponse(
            {
                "access_token": idp_access_token,
                "token_type": "Bearer",
                "expires_in": 3600,  # Typical access token lifetime
                # Note: We don't return the master refresh token!
                # MCP client would need to re-authenticate when token expires
            }
        )

    elif grant_type == "refresh_token":
        # Refresh token grant (not implemented in ADR-004 initial version)
        return JSONResponse(
            {
                "error": "unsupported_grant_type",
                "error_description": "refresh_token grant not yet implemented",
            },
            status_code=400,
        )

    else:
        return JSONResponse(
            {
                "error": "unsupported_grant_type",
                "error_description": f"grant_type '{grant_type}' is not supported",
            },
            status_code=400,
        )


async def oauth_authorize_nextcloud(
    request: Request,
) -> RedirectResponse | JSONResponse:
    """
    OAuth authorization endpoint for Flow 2: Resource Provisioning.

    This endpoint is used by the provision_nextcloud_access MCP tool
    to initiate delegated resource access to Nextcloud. Requires a separate
    login flow outside of the MCP session.

    Only available when Progressive Consent is enabled (opt-in).

    Query parameters:
        state: Session state for tracking

    Returns:
        302 redirect to IdP authorization endpoint
    """
    # Check if Progressive Consent is enabled (opt-in, defaults to false)
    enable_progressive = (
        os.getenv("ENABLE_PROGRESSIVE_CONSENT", "false").lower() == "true"
    )
    if not enable_progressive:
        return JSONResponse(
            {
                "error": "not_enabled",
                "error_description": "Progressive Consent mode is not enabled",
            },
            status_code=400,
        )

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

    # Define resource access scopes
    scopes = (
        "openid profile email offline_access "
        "notes:read notes:write "
        "calendar:read calendar:write "
        "contacts:read contacts:write "
        "files:read files:write"
    )

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
