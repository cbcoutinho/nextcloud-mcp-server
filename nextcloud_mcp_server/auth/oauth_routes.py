"""
OAuth 2.0 Login Routes for ADR-004 (Offline Access Architecture) and ADR-023 (AS Proxy)

Implements dual OAuth flows with optional offline access provisioning:

Flow 1: Client Authentication (AS Proxy mode, ADR-023)
- MCP server acts as its own OAuth Authorization Server
- Proxies DCR, authorization, and token endpoints to Nextcloud
- Uses MCP server's own client_id so tokens have correct audience
- Client exchanges proxy authorization code for Nextcloud token

Flow 2: Resource Provisioning - MCP server gets delegated Nextcloud access
- Triggered by user calling provision_nextcloud_access tool
- Server requests: openid, profile, email scopes, offline_access
- Separate login flow outside MCP session, results in browser login for user
- Token audience (aud): "nextcloud", redirect/callback to mcp server
- Server receives refresh token for offline access
- Client never sees this token

"""

import hashlib
import logging
import os
import secrets
import time
from base64 import b64decode, urlsafe_b64encode
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlencode
from urllib.parse import urlparse as parse_url

import jwt
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from nextcloud_mcp_server.auth.browser_oauth_routes import oauth_login_callback
from nextcloud_mcp_server.auth.client_registry import get_client_registry
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory proxy code store for AS proxy flow (ADR-023)
# Proxy codes are ephemeral (60s TTL), single-instance, so in-memory is fine.
# ---------------------------------------------------------------------------


@dataclass
class ProxyCodeEntry:
    """Stores state for a proxy authorization code issued by the AS proxy.

    Proxy codes have a 60-second TTL as a security mitigation: they are
    single-use, ephemeral codes that bridge the AS proxy callback and the
    client's token exchange. The short window limits replay risk.
    """

    client_id: str
    client_redirect_uri: str
    client_state: str
    code_challenge: str
    code_challenge_method: str
    nc_token_response: dict[str, Any]  # Full JSON token response from Nextcloud
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 60)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


# Server-side state for AS proxy authorize → callback mapping
@dataclass
class ASProxySession:
    """Stores state between /oauth/authorize and the Nextcloud callback.

    Sessions have a 600-second (10 minute) TTL to allow time for the user
    to complete the browser-based authorization flow.
    """

    client_id: str
    client_redirect_uri: str
    client_state: str
    code_challenge: str
    code_challenge_method: str
    requested_scopes: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 600)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


# In-memory stores (single-instance, ephemeral)
_proxy_codes: dict[str, ProxyCodeEntry] = {}
_as_proxy_sessions: dict[str, ASProxySession] = {}

# OIDC discovery document cache (URL → (expires_at, data))
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_DISCOVERY_CACHE_TTL = 300  # 5 minutes

# DCR rate limiting (IP → [timestamps])
_dcr_rate_limit: dict[str, list[float]] = {}
_DCR_RATE_LIMIT_MAX = 10  # max requests
_DCR_RATE_LIMIT_WINDOW = 60  # per 60 seconds


async def _get_cached_discovery(url: str) -> dict[str, Any]:
    """Fetch OIDC discovery document with caching (5-minute TTL)."""
    now = time.time()
    if url in _discovery_cache:
        expires_at, data = _discovery_cache[url]
        if now < expires_at:
            return data
    async with nextcloud_httpx_client() as http_client:
        response = await http_client.get(url)
        response.raise_for_status()
        data = response.json()
    _discovery_cache[url] = (now + _DISCOVERY_CACHE_TTL, data)
    return data


def _cleanup_expired_proxy_codes() -> None:
    """Remove expired proxy codes and sessions."""
    now = time.time()
    expired_codes = [k for k, v in _proxy_codes.items() if now > v.expires_at]
    for k in expired_codes:
        del _proxy_codes[k]
    expired_sessions = [k for k, v in _as_proxy_sessions.items() if now > v.expires_at]
    for k in expired_sessions:
        del _as_proxy_sessions[k]


async def oauth_authorize(request: Request) -> RedirectResponse | JSONResponse:
    """
    OAuth authorization endpoint — AS Proxy intermediary (ADR-023).

    The MCP server acts as its own OAuth Authorization Server, proxying
    the authorization to Nextcloud. This ensures tokens have the correct
    audience (MCP server's client_id) instead of the MCP client's client_id.

    Flow:
    1. Client sends authorize request with its own client_id + PKCE
    2. Server stores client params, generates server-side state
    3. Server redirects to Nextcloud with MCP server's own client_id
    4. Nextcloud callback returns to /oauth/callback (flow_type=as_proxy)
    5. Server exchanges code, generates proxy_code for client
    6. Client exchanges proxy_code at /oauth/token

    Query parameters:
        response_type: Must be "code"
        client_id: MCP client identifier (required)
        redirect_uri: Client's localhost redirect URI (required)
        scope: Requested scopes (optional, defaults to "openid profile email")
        state: CSRF protection state (required)
        code_challenge: PKCE code challenge from client (required)
        code_challenge_method: PKCE method, must be "S256" (required)

    Returns:
        302 redirect to Nextcloud authorization endpoint
    """
    # Clean up expired entries periodically
    _cleanup_expired_proxy_codes()

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

    # Validate redirect_uri scheme security (OAuth 2.1):
    # - Localhost: HTTP allowed (RFC 8252 loopback exception for native clients)
    # - Remote hosts: HTTPS required (cloud clients like Claude AI)
    parsed_redirect = parse_url(redirect_uri)
    is_loopback = parsed_redirect.hostname in ("localhost", "127.0.0.1")
    if not (is_loopback or parsed_redirect.scheme == "https"):
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "redirect_uri must use HTTPS for non-localhost URIs",
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

    # Validate client_id (required)
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

    oauth_config = oauth_ctx["config"]

    # AS Proxy: Store client's params and redirect to Nextcloud with MCP server's credentials
    # PKCE is validated locally when the client exchanges the proxy_code at /oauth/token.
    # We do NOT forward PKCE to Nextcloud — the MCP server is a confidential client.
    server_state = secrets.token_urlsafe(32)

    requested_scope = request.query_params.get("scope", "")
    default_scopes = "openid profile email"
    resource_scopes = oauth_config.get("scopes", "")
    scopes = f"{default_scopes} {resource_scopes}".strip()
    if requested_scope:
        # Merge client-requested scopes with server defaults
        all_scopes = set(scopes.split()) | set(requested_scope.split())
        scopes = " ".join(sorted(all_scopes))

    # Store session for callback
    _as_proxy_sessions[server_state] = ASProxySession(
        client_id=client_id,
        client_redirect_uri=redirect_uri,
        client_state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        requested_scopes=scopes,
    )

    # Use MCP server's own client_id with Nextcloud
    mcp_server_client_id = os.getenv(
        "MCP_SERVER_CLIENT_ID", oauth_config.get("client_id")
    )
    mcp_server_url = oauth_config["mcp_server_url"]
    callback_uri = f"{mcp_server_url}/oauth/callback"

    logger.info("AS Proxy: Intermediary authorization flow")
    logger.info(f"  Client: {client_id}")
    logger.info(f"  MCP server client_id: {mcp_server_client_id}")
    logger.info(f"  Server callback: {callback_uri}")
    logger.info(f"  Scopes: {scopes}")

    # Discover Nextcloud authorization endpoint
    discovery_url = oauth_config.get("discovery_url")
    if not discovery_url:
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "OAuth discovery URL not configured",
            },
            status_code=500,
        )

    discovery = await _get_cached_discovery(discovery_url)
    authorization_endpoint = discovery["authorization_endpoint"]

    # Replace internal Docker hostname with public URL for browser access
    public_issuer = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL")
    if public_issuer:
        internal_parsed = parse_url(oauth_config["nextcloud_host"])
        auth_parsed = parse_url(authorization_endpoint)

        if auth_parsed.hostname == internal_parsed.hostname:
            public_parsed = parse_url(public_issuer)
            authorization_endpoint = (
                f"{public_parsed.scheme}://{public_parsed.netloc}{auth_parsed.path}"
            )
            if auth_parsed.query:
                authorization_endpoint += f"?{auth_parsed.query}"
            logger.info(
                f"Rewrote authorization endpoint for browser access: {authorization_endpoint}"
            )

    # Redirect to Nextcloud with MCP server's own client_id (no PKCE — confidential client)
    idp_params = {
        "client_id": mcp_server_client_id,
        "redirect_uri": callback_uri,
        "response_type": "code",
        "scope": scopes,
        "state": server_state,
        "prompt": "consent",
        "resource": f"{mcp_server_url}/mcp",  # MCP server audience
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
    callback_uri = f"{mcp_server_url}/oauth/callback"

    # Flow 2: Server only needs identity + offline access (no resource scopes)
    # Resource scopes are requested by client in Flow 1
    scopes = "openid profile email offline_access"

    # Generate PKCE values (required by Nextcloud OIDC)
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = urlsafe_b64encode(digest).decode().rstrip("=")

    # Store code_verifier in session for retrieval during callback
    storage = oauth_ctx["storage"]
    await storage.store_oauth_session(
        session_id=state,
        client_id=mcp_server_client_id,
        client_redirect_uri=callback_uri,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method="S256",
        mcp_authorization_code=code_verifier,  # Store code_verifier here temporarily
        flow_type="flow2",
        ttl_seconds=600,  # 10 minutes
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

    discovery = await _get_cached_discovery(discovery_url)
    authorization_endpoint = discovery["authorization_endpoint"]

    # Fix internal hostname for browser access
    public_issuer = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL")
    if public_issuer:
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
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
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

    # Retrieve code_verifier from session storage (PKCE required by Nextcloud OIDC)
    code_verifier = ""
    oauth_session = await storage.get_oauth_session(state)
    if oauth_session:
        # code_verifier was stored in mcp_authorization_code field
        code_verifier = oauth_session.get("mcp_authorization_code", "")
        logger.info(
            f"Retrieved code_verifier for Flow 2 callback (state={state[:16]}...)"
        )

    # Exchange code for tokens
    mcp_server_client_id = os.getenv(
        "MCP_SERVER_CLIENT_ID", oauth_config.get("client_id")
    )
    mcp_server_client_secret = os.getenv(
        "MCP_SERVER_CLIENT_SECRET", oauth_config.get("client_secret")
    )
    mcp_server_url = oauth_config["mcp_server_url"]
    callback_uri = f"{mcp_server_url}/oauth/callback"

    discovery_url = oauth_config.get("discovery_url")
    if not discovery_url:
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "OIDC discovery URL not configured",
            },
            status_code=500,
        )

    discovery = await _get_cached_discovery(discovery_url)
    token_endpoint = discovery["token_endpoint"]

    # Build token exchange params
    token_params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_uri,
        "client_id": mcp_server_client_id,
        "client_secret": mcp_server_client_secret,
    }

    # Add code_verifier for PKCE (required by Nextcloud OIDC)
    if code_verifier:
        token_params["code_verifier"] = code_verifier

    # Exchange code for tokens
    async with nextcloud_httpx_client() as http_client:
        response = await http_client.post(
            token_endpoint,
            data=token_params,
        )
        response.raise_for_status()
        token_data = response.json()

    refresh_token = token_data.get("refresh_token")
    id_token = token_data.get("id_token")

    # Decode ID token to get user info
    logger.info("=" * 60)
    logger.info("oauth_callback_nextcloud: Extracting user_id from ID token")
    logger.info("=" * 60)
    try:
        userinfo = jwt.decode(id_token, options={"verify_signature": False})
        user_id = userinfo.get("sub")
        username = userinfo.get("preferred_username") or userinfo.get("email")
        logger.info("  ✓ ID token decode SUCCESSFUL")
        logger.info(f"  Extracted user_id: {user_id}")
        logger.info(f"  Username: {username}")
        logger.info(f"  ID token payload keys: {list(userinfo.keys())}")
        logger.info(f"Flow 2: User {username} provisioned resource access")
    except Exception as e:
        logger.error(f"  ✗ ID token decode FAILED: {type(e).__name__}: {e}")
        user_id = "unknown"
        logger.error(f"  Using fallback user_id: {user_id}")

    # Store master refresh token for Flow 2
    if refresh_token:
        # Parse granted scopes from token response
        granted_scopes = (
            token_data.get("scope", "").split() if token_data.get("scope") else None
        )

        # Calculate refresh token expiration from token response
        refresh_expires_in = token_data.get("refresh_expires_in")
        refresh_expires_at = None
        if refresh_expires_in:
            refresh_expires_at = int(time.time()) + refresh_expires_in
            logger.info(f"  refresh_expires_in: {refresh_expires_in}s")
            logger.info(f"  refresh_expires_at: {refresh_expires_at}")

        logger.info("Storing refresh token:")
        logger.info(f"  user_id: {user_id}")
        logger.info("  flow_type: flow2")
        logger.info("  token_audience: nextcloud")
        logger.info(f"  provisioning_client_id: {state[:16]}...")
        logger.info(f"  scopes: {granted_scopes}")
        logger.info(f"  expires_at: {refresh_expires_at}")

        await storage.store_refresh_token(
            user_id=user_id,
            refresh_token=refresh_token,
            flow_type="flow2",
            token_audience="nextcloud",
            provisioning_client_id=state,  # Store which client initiated provisioning
            scopes=granted_scopes,
            expires_at=refresh_expires_at,
        )
        logger.info(f"✓ Stored Flow 2 master refresh token for user {user_id}")
        logger.info("=" * 60)

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

    return HTMLResponse(content=success_html, status_code=200)


async def oauth_callback(request: Request):
    """
    Unified OAuth callback endpoint supporting multiple flows.

    This endpoint consolidates all OAuth callback handling into a single URL.
    The flow type is determined by looking up the OAuth session using the
    state parameter.

    This simplifies IdP configuration by requiring only one callback URL
    to be registered: /oauth/callback

    Query parameters:
        code: Authorization code from IdP
        state: CSRF protection state (also used to lookup flow type)
        error: Error code (if authorization failed)

    Returns:
        Response from the appropriate flow handler
    """
    # Get state parameter to lookup OAuth session
    state = request.query_params.get("state")
    if not state:
        logger.warning("Unified callback called without state parameter")
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "state parameter is required",
            },
            status_code=400,
        )

    # Check AS proxy sessions first (in-memory, ADR-023)
    if state in _as_proxy_sessions:
        logger.info("Routing to AS proxy callback (ADR-023)")
        return await _oauth_callback_as_proxy(request, state)

    # Lookup OAuth session to determine flow type
    oauth_ctx = request.app.state.oauth_context
    if not oauth_ctx:
        logger.error("OAuth context not available")
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "OAuth not configured on server",
            },
            status_code=500,
        )

    storage = oauth_ctx["storage"]
    oauth_session = await storage.get_oauth_session(state)

    # Determine flow type from session, default to "browser" for backwards compatibility
    flow_type = (
        oauth_session.get("flow_type", "browser") if oauth_session else "browser"
    )

    logger.info(f"Unified callback: flow_type={flow_type} (from session lookup)")

    if flow_type == "flow2":
        # Flow 2: Resource Provisioning - MCP server gets delegated Nextcloud access
        logger.info("Routing to Flow 2 (resource provisioning)")
        return await oauth_callback_nextcloud(request)

    elif flow_type == "browser":
        # Browser UI Login - establish browser session for /user/page access
        logger.info("Routing to browser login flow")
        return await oauth_login_callback(request)

    else:
        # Unknown flow type
        logger.warning(f"Unknown flow_type in OAuth session: {flow_type}")
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": f"Unknown flow type: {flow_type}",
            },
            status_code=400,
        )


# ---------------------------------------------------------------------------
# AS Proxy endpoints (ADR-023)
# ---------------------------------------------------------------------------


async def _oauth_callback_as_proxy(
    request: Request, server_state: str
) -> RedirectResponse | JSONResponse:
    """
    Handle Nextcloud callback for the AS proxy flow.

    Exchanges the Nextcloud auth code for tokens server-side, generates a
    proxy authorization code, and redirects back to the client.
    """
    # Check for errors from Nextcloud
    error = request.query_params.get("error")
    if error:
        error_description = request.query_params.get(
            "error_description", "Authorization failed"
        )
        logger.error(f"AS proxy callback error: {error} - {error_description}")

        # Retrieve session to redirect back to client with error
        session = _as_proxy_sessions.pop(server_state, None)
        if session:
            params = urlencode(
                {
                    "error": error,
                    "error_description": error_description,
                    "state": session.client_state,
                }
            )
            return RedirectResponse(
                f"{session.client_redirect_uri}?{params}", status_code=302
            )
        return JSONResponse(
            {"error": error, "error_description": error_description},
            status_code=400,
        )

    code = request.query_params.get("code")
    if not code:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "code parameter is required",
            },
            status_code=400,
        )

    # Retrieve and consume the session (one-time use)
    session = _as_proxy_sessions.pop(server_state, None)
    if not session:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "Unknown or expired server state",
            },
            status_code=400,
        )

    if session.is_expired:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "Authorization session expired",
            },
            status_code=400,
        )

    # Get OAuth context
    oauth_ctx = request.app.state.oauth_context
    oauth_config = oauth_ctx["config"]

    mcp_server_client_id = os.getenv(
        "MCP_SERVER_CLIENT_ID", oauth_config.get("client_id")
    )
    mcp_server_client_secret = os.getenv(
        "MCP_SERVER_CLIENT_SECRET", oauth_config.get("client_secret")
    )

    if not mcp_server_client_id or not mcp_server_client_secret:
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "MCP server OAuth credentials not configured",
            },
            status_code=500,
        )

    mcp_server_url = oauth_config["mcp_server_url"]
    callback_uri = f"{mcp_server_url}/oauth/callback"

    # Discover token endpoint
    discovery_url = oauth_config.get("discovery_url")
    if not discovery_url:
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "OIDC discovery URL not configured",
            },
            status_code=500,
        )

    discovery = await _get_cached_discovery(discovery_url)
    token_endpoint = discovery["token_endpoint"]

    # Exchange auth code with Nextcloud (server-side, confidential client, no PKCE)
    token_params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_uri,
        "client_id": mcp_server_client_id,
        "client_secret": mcp_server_client_secret,
    }

    async with nextcloud_httpx_client() as http_client:
        response = await http_client.post(token_endpoint, data=token_params)

    if response.status_code != 200:
        logger.error(
            f"AS proxy token exchange failed: {response.status_code} {response.text}"
        )
        params = urlencode(
            {
                "error": "server_error",
                "error_description": "Failed to exchange authorization code",
                "state": session.client_state,
            }
        )
        return RedirectResponse(
            f"{session.client_redirect_uri}?{params}", status_code=302
        )

    nc_token_response = response.json()

    logger.info(
        "AS proxy: Successfully exchanged code for Nextcloud token "
        f"(token_type={nc_token_response.get('token_type')})"
    )

    # Generate a proxy authorization code for the client
    proxy_code = secrets.token_urlsafe(32)
    _proxy_codes[proxy_code] = ProxyCodeEntry(
        client_id=session.client_id,
        client_redirect_uri=session.client_redirect_uri,
        client_state=session.client_state,
        code_challenge=session.code_challenge,
        code_challenge_method=session.code_challenge_method,
        nc_token_response=nc_token_response,
    )

    # Redirect back to client with proxy_code and client's original state
    redirect_params = urlencode({"code": proxy_code, "state": session.client_state})
    redirect_url = f"{session.client_redirect_uri}?{redirect_params}"

    logger.info(
        f"AS proxy: Redirecting to client with proxy_code (client_id={session.client_id})"
    )
    return RedirectResponse(redirect_url, status_code=302)


def _extract_basic_auth(request: Request) -> tuple[str | None, str | None]:
    """Extract client_id and client_secret from HTTP Basic Auth header (RFC 6749 §2.3.1)."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Basic "):
        return None, None
    try:
        decoded = b64decode(auth_header[6:]).decode("utf-8")
        client_id, _, client_secret = decoded.partition(":")
        return unquote(client_id), unquote(client_secret) if client_secret else None
    except Exception:
        return None, None


def _verify_pkce_s256(code_verifier: str, code_challenge: str) -> bool:
    """Verify PKCE S256 code_verifier against stored code_challenge.

    Per RFC 7636 Section 4.6:
    code_challenge = BASE64URL(SHA256(ASCII(code_verifier)))
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed_challenge = urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return secrets.compare_digest(computed_challenge, code_challenge)


async def oauth_token_endpoint(request: Request) -> JSONResponse:
    """
    OAuth token endpoint for AS proxy (ADR-023).

    Handles:
    - grant_type=authorization_code: Exchange proxy_code for Nextcloud token
    - grant_type=refresh_token: Proxy refresh request to Nextcloud

    Form parameters:
        grant_type: "authorization_code" or "refresh_token"
        code: Proxy authorization code (for authorization_code grant)
        redirect_uri: Must match the original redirect_uri
        code_verifier: PKCE verifier (for authorization_code grant)
        client_id: Client identifier
        client_secret: Client secret (optional for public clients)
        refresh_token: Refresh token (for refresh_token grant)
    """
    # Parse form body
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        return await _token_authorization_code(request, form)
    elif grant_type == "refresh_token":
        return await _token_refresh(request, form)
    else:
        return JSONResponse(
            {
                "error": "unsupported_grant_type",
                "error_description": f"Unsupported grant_type: {grant_type}",
            },
            status_code=400,
        )


async def _token_authorization_code(request: Request, form) -> JSONResponse:
    """Handle authorization_code grant type at the token endpoint."""
    code = form.get("code")
    redirect_uri = form.get("redirect_uri")
    code_verifier = form.get("code_verifier")
    client_id = form.get("client_id")

    # RFC 6749 §2.3.1: clients may authenticate via HTTP Basic Auth
    if not client_id:
        client_id, _ = _extract_basic_auth(request)

    logger.debug(
        "AS proxy token: received code=%s client_id=%s redirect_uri=%s "
        "code_verifier=%s",
        code[:8] + "..." if code else None,
        client_id,
        redirect_uri,
        "present" if code_verifier else "missing",
    )

    if not code:
        logger.warning("AS proxy token: Missing 'code' parameter")
        return JSONResponse(
            {"error": "invalid_request", "error_description": "code is required"},
            status_code=400,
        )

    # Look up and consume proxy code (one-time use)
    entry = _proxy_codes.pop(code, None)
    if not entry:
        logger.warning(
            "AS proxy token: Invalid or expired code (active_codes=%d)",
            len(_proxy_codes),
        )
        return JSONResponse(
            {
                "error": "invalid_grant",
                "error_description": "Invalid or expired authorization code",
            },
            status_code=400,
        )

    if entry.is_expired:
        age = time.time() - entry.created_at
        logger.warning("AS proxy token: Proxy code expired (age=%.1fs, TTL=60s)", age)
        return JSONResponse(
            {
                "error": "invalid_grant",
                "error_description": "Authorization code has expired",
            },
            status_code=400,
        )

    # Validate client_id (required per RFC 6749 Section 4.1.3)
    if not client_id:
        logger.warning("AS proxy token: Missing 'client_id' parameter")
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "client_id is required",
            },
            status_code=400,
        )

    if client_id != entry.client_id:
        logger.warning(
            "AS proxy token: client_id mismatch (got=%s, expected=%s)",
            client_id,
            entry.client_id,
        )
        return JSONResponse(
            {
                "error": "invalid_grant",
                "error_description": "client_id mismatch",
            },
            status_code=400,
        )

    # Validate redirect_uri (required per RFC 6749 Section 4.1.3)
    if not redirect_uri:
        logger.warning("AS proxy token: Missing 'redirect_uri' parameter")
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "redirect_uri is required",
            },
            status_code=400,
        )

    if redirect_uri != entry.client_redirect_uri:
        logger.warning(
            "AS proxy token: redirect_uri mismatch (got=%s, expected=%s)",
            redirect_uri,
            entry.client_redirect_uri,
        )
        return JSONResponse(
            {
                "error": "invalid_grant",
                "error_description": "redirect_uri mismatch",
            },
            status_code=400,
        )

    # Verify PKCE (always required — oauth_authorize mandates code_challenge)
    if not entry.code_challenge:
        logger.error("AS proxy token: code_challenge missing from stored entry")
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "Internal state error: missing PKCE challenge",
            },
            status_code=500,
        )

    if not code_verifier:
        logger.warning("AS proxy token: Missing 'code_verifier' (PKCE required)")
        return JSONResponse(
            {
                "error": "invalid_grant",
                "error_description": "code_verifier is required (PKCE)",
            },
            status_code=400,
        )

    if not _verify_pkce_s256(code_verifier, entry.code_challenge):
        logger.warning(f"PKCE verification failed for client {entry.client_id}")
        return JSONResponse(
            {
                "error": "invalid_grant",
                "error_description": "PKCE verification failed",
            },
            status_code=400,
        )

    logger.info(
        f"AS proxy token: Returning Nextcloud token for client {entry.client_id}"
    )

    # Return the stored Nextcloud token response directly
    return JSONResponse(entry.nc_token_response)


async def _token_refresh(request: Request, form) -> JSONResponse:
    """Handle refresh_token grant type by proxying to Nextcloud."""
    refresh_token = form.get("refresh_token")
    if not refresh_token:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "refresh_token is required",
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

    mcp_server_client_id = os.getenv(
        "MCP_SERVER_CLIENT_ID", oauth_config.get("client_id")
    )
    mcp_server_client_secret = os.getenv(
        "MCP_SERVER_CLIENT_SECRET", oauth_config.get("client_secret")
    )

    if not mcp_server_client_id or not mcp_server_client_secret:
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "MCP server OAuth credentials not configured",
            },
            status_code=500,
        )

    mcp_server_url = oauth_config["mcp_server_url"]

    # Discover token endpoint
    discovery_url = oauth_config.get("discovery_url")
    if not discovery_url:
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": "OIDC discovery URL not configured",
            },
            status_code=500,
        )

    discovery = await _get_cached_discovery(discovery_url)
    token_endpoint = discovery["token_endpoint"]

    # Proxy refresh request to Nextcloud
    token_params = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": mcp_server_client_id,
        "client_secret": mcp_server_client_secret,
        "resource": f"{mcp_server_url}/mcp",
    }

    async with nextcloud_httpx_client() as http_client:
        response = await http_client.post(token_endpoint, data=token_params)

    if response.status_code != 200:
        logger.error(
            f"AS proxy token refresh failed: {response.status_code} {response.text}"
        )
        return JSONResponse(
            {
                "error": "invalid_grant",
                "error_description": "Token refresh failed",
            },
            status_code=response.status_code,
        )

    return JSONResponse(response.json())


async def oauth_register_proxy(request: Request) -> JSONResponse:
    """
    DCR proxy endpoint for AS proxy (ADR-023).

    Proxies Dynamic Client Registration requests to Nextcloud's OIDC endpoint
    and registers the resulting client in the local ClientRegistry.

    This allows MCP clients to register via the MCP server (their AS) rather
    than directly with Nextcloud (which would produce tokens with wrong audience).
    """
    # Parse JSON body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "Request body must be valid JSON",
            },
            status_code=400,
        )

    # Get OAuth context for Nextcloud endpoint
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
    nextcloud_host = oauth_config["nextcloud_host"]

    # Rate limit DCR requests per client IP
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    timestamps = _dcr_rate_limit.get(client_ip, [])
    # Remove timestamps outside the window
    timestamps = [t for t in timestamps if now - t < _DCR_RATE_LIMIT_WINDOW]
    if len(timestamps) >= _DCR_RATE_LIMIT_MAX:
        logger.warning(f"DCR rate limit exceeded for {client_ip}")
        return JSONResponse(
            {
                "error": "too_many_requests",
                "error_description": "Rate limit exceeded for client registration",
            },
            status_code=429,
            headers={"Retry-After": str(_DCR_RATE_LIMIT_WINDOW)},
        )
    timestamps.append(now)
    _dcr_rate_limit[client_ip] = timestamps

    # Discover registration endpoint from OIDC discovery (prefer over hardcoded path)
    discovery_url = oauth_config.get("discovery_url")
    if discovery_url:
        try:
            discovery = await _get_cached_discovery(discovery_url)
            registration_endpoint = discovery.get(
                "registration_endpoint", f"{nextcloud_host}/apps/oidc/register"
            )
        except Exception:
            logger.warning(
                "Failed to fetch OIDC discovery for DCR endpoint, using fallback"
            )
            registration_endpoint = f"{nextcloud_host}/apps/oidc/register"
    else:
        registration_endpoint = f"{nextcloud_host}/apps/oidc/register"

    logger.info(f"DCR proxy: Forwarding registration to {registration_endpoint}")

    async with nextcloud_httpx_client() as http_client:
        response = await http_client.post(
            registration_endpoint,
            json=body,
            headers={"Content-Type": "application/json"},
        )

    if response.status_code not in (200, 201):
        logger.error(
            f"DCR proxy: Nextcloud registration failed: {response.status_code} {response.text}"
        )
        return JSONResponse(
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {
                "error": "server_error",
                "error_description": f"Upstream registration failed: {response.status_code}",
            },
            status_code=response.status_code,
        )

    nc_response = response.json()
    new_client_id = nc_response.get("client_id")

    if new_client_id:
        # Register in local ClientRegistry so /oauth/authorize accepts it
        redirect_uris = nc_response.get("redirect_uris", [])
        client_name = nc_response.get("client_name", "")
        registry = get_client_registry()
        registry.register_proxy_client(
            client_id=new_client_id,
            redirect_uris=redirect_uris,
            name=client_name,
        )
        logger.info(f"DCR proxy: Registered client {new_client_id} in local registry")

    return JSONResponse(nc_response, status_code=response.status_code)


async def oauth_as_metadata(request: Request) -> JSONResponse:
    """
    RFC 8414 OAuth Authorization Server Metadata endpoint (ADR-023).

    Advertises the MCP server as its own OAuth Authorization Server so that
    MCP clients (e.g., Claude Code) authenticate through the proxy rather
    than directly with Nextcloud.
    """
    mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")

    # Dynamically discover scopes from registered tools if available
    scopes_supported = ["openid", "profile", "email"]
    app_scopes = getattr(request.app.state, "supported_scopes", None)
    if app_scopes:
        scopes_supported = app_scopes

    return JSONResponse(
        {
            "issuer": mcp_server_url,
            "authorization_endpoint": f"{mcp_server_url}/oauth/authorize",
            "token_endpoint": f"{mcp_server_url}/oauth/token",
            "registration_endpoint": f"{mcp_server_url}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
                "none",
            ],
            "scopes_supported": scopes_supported,
        }
    )
