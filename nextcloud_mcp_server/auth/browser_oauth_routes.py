"""Browser-based OAuth login routes for admin UI.

Separate from MCP OAuth flow - these routes establish browser sessions
for accessing admin UI endpoints like /app.
"""

import hashlib
import logging
import secrets
import time
from base64 import urlsafe_b64encode
from html import escape as html_escape
from urllib.parse import urlencode
from urllib.parse import urlparse as parse_url

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from nextcloud_mcp_server.auth.storage import get_shared_storage
from nextcloud_mcp_server.auth.token_utils import (
    IdTokenVerificationError,
    get_oidc_discovery,
    verify_id_token,
)
from nextcloud_mcp_server.auth.userinfo_routes import (
    _get_userinfo_endpoint,
    _query_idp_userinfo,
)
from nextcloud_mcp_server.config import get_settings

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


def _normalise_origin(raw: str) -> tuple[str, str, int | None]:
    """Return (scheme, hostname, port) with default HTTP/HTTPS ports stripped.

    Browsers omit default ports in Origin headers (RFC 6454 §6.2), so a
    raw netloc string comparison falsely rejects requests whenever
    ``mcp_server_url`` is configured with an explicit ``:443`` / ``:80``
    (or vice versa).
    """
    parsed = parse_url(raw)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    return (scheme, hostname, port)


def _origin_matches_self(request: Request, oauth_ctx: dict) -> bool:
    """Return True when Origin/Referer is missing or matches our own host.

    Used to gate POST /oauth/logout against cross-origin form submissions
    (PR #758 round-3 review hardening). Per OWASP CSRF cheat sheet, the
    policy is:
      - If neither Origin nor Referer is set, allow (same-origin POST in
        privacy-conscious browsers may strip both).
      - Otherwise, the (scheme, hostname, port) tuple of the first present
        header must equal the same tuple of the configured
        ``mcp_server_url``. Default ports (80/443) are normalised away
        before comparison so RFC-6454-compliant browsers — which omit
        default ports in Origin — aren't rejected.
    """
    cfg = oauth_ctx.get("config") or oauth_ctx
    mcp_server_url = cfg.get("mcp_server_url")
    if not mcp_server_url:
        # Fail closed (PR #758 round-3 finding 2): a future code path that
        # leaves ``mcp_server_url`` unset would otherwise silently disable
        # CSRF protection on /oauth/logout. Blocking the logout is
        # recoverable — the user just re-logs-in once the misconfiguration
        # is fixed — and the error log makes the cause monitorable.
        logger.error(
            "CSRF check failed on /oauth/logout: mcp_server_url not "
            "configured in oauth_context — set NEXTCLOUD_MCP_SERVER_URL"
        )
        return False

    expected = _normalise_origin(mcp_server_url)
    raw = request.headers.get("origin") or request.headers.get("referer")
    if not raw:
        return True
    return _normalise_origin(raw) == expected


def _safe_next_url(raw: str | None, default: str) -> str:
    """Return a path-only redirect target, falling back to *default*.

    Blocks open-redirect abuse via the ``?next=`` query parameter on
    ``/oauth/login`` and ``/oauth/logout`` (and the round-tripped
    ``client_redirect_uri`` stored on the oauth_session). A safe target:
      - starts with a single ``/`` (so it's a path on this server)
      - does NOT start with ``//`` (which would be protocol-relative)
      - has no whitespace or control characters that could trick browsers

    Anything else returns *default*.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return default
    if any(c.isspace() or ord(c) < 0x20 for c in raw):
        return default
    return raw


def _should_use_secure_cookies() -> bool:
    """Determine if cookies should have the Secure flag.

    Reads ``settings.cookie_secure`` first (set via the ``COOKIE_SECURE``
    env var). Falls back to auto-detecting from the MCP server's own URL
    scheme — the cookie is issued by THIS server, so the Secure flag must
    reflect THIS server's transport, not Nextcloud's. (Split-scheme
    deployments — HTTPS Nextcloud + plain-HTTP MCP sidecar, or vice
    versa — would otherwise get the wrong answer.)
    """
    settings = get_settings()
    raw = settings.cookie_secure
    if raw is not None:
        # Dynaconf normally coerces "true"/"false"/"1"/"0", but tests or
        # direct ``settings.set`` calls can bypass that — bool("false") is
        # True. Normalise explicitly so an unexpected string never flips
        # cookies to Secure on plain HTTP (round-6 review).
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("0", "false", "no", "off", "")
    mcp_server_url = settings.nextcloud_mcp_server_url or ""
    return mcp_server_url.startswith("https://")


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
        # BasicAuth mode - no login needed, redirect to app
        return RedirectResponse("/app", status_code=302)

    # Fall back to the always-initialized shared storage singleton when the
    # OAuth context carries no storage — login_flow with offline access off
    # (the default) leaves oauth_context["storage"] None (GH #1068).
    storage = oauth_ctx.get("storage") or await get_shared_storage()
    oauth_client = oauth_ctx["oauth_client"]
    oauth_config = oauth_ctx["config"]

    # Demoted to DEBUG (PR #758 nit a) — these previously leaked the
    # full set of config keys + the client_id at INFO on every login.
    logger.debug("oauth_login called - oauth_config keys: %s", oauth_config.keys())
    logger.debug("oauth_login called - client_id: %s", oauth_config.get("client_id"))
    logger.debug("oauth_login called - oauth_client: %s", oauth_client is not None)

    # Get redirect URL from query params (default to /app). Validated at
    # write-time so we never store an attacker-controlled absolute URL on
    # the oauth_session row (issue #758 finding 3).
    next_url = _safe_next_url(request.query_params.get("next"), "/app")
    logger.debug("oauth_login - next_url: %s", next_url)

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)

    # Generate OIDC nonce so the ID token returned on callback can be bound
    # to THIS auth request (PR #758 finding 2). Without a nonce, an attacker
    # who acquired a separate valid ID token could replay it inside this
    # flow.
    nonce = secrets.token_urlsafe(32)

    # Build OAuth authorization URL
    mcp_server_url = oauth_config["mcp_server_url"]
    callback_uri = f"{mcp_server_url}/oauth/callback"

    # Request only basic OIDC scopes for browser session.
    # offline_access is added conditionally below based on IdP discovery.
    # Note: Nextcloud app scopes (notes.read, etc.) are for MCP client access tokens,
    # not for the MCP server's own browser authentication
    scopes = "openid profile email"

    # Generate PKCE values for ALL modes (both external and integrated IdP require PKCE)
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = urlsafe_b64encode(digest).decode().rstrip("=")

    # Store code_verifier + nonce in session for retrieval during callback
    # (using state as key)
    await storage.store_oauth_session(
        session_id=state,  # Use state as session ID
        client_id="browser-ui",
        client_redirect_uri=next_url,  # Store the redirect URL for after auth
        state=state,
        code_challenge=code_challenge,
        code_challenge_method="S256",
        # `mcp_authorization_code` field reused to store the PKCE
        # code_verifier (one-time-use). Renaming the column requires a
        # schema migration.
        mcp_authorization_code=code_verifier,
        nonce=nonce,
        flow_type="browser",
        ttl_seconds=600,  # 10 minutes
    )

    if oauth_client:
        # External IdP mode (Keycloak)
        if not oauth_client.authorization_endpoint:
            await oauth_client.discover()

        # Check if IdP supports offline_access via server metadata from discovery
        idp_metadata = getattr(oauth_client, "server_metadata", None) or {}
        idp_scopes = idp_metadata.get("scopes_supported")
        if idp_scopes is None or "offline_access" in idp_scopes:
            scopes += " offline_access"

        # Get Nextcloud resource URI for audience (background sync needs Nextcloud-scoped tokens)
        nextcloud_resource_uri = oauth_config.get(
            "nextcloud_resource_uri", oauth_config.get("nextcloud_host")
        )

        idp_params = {
            "client_id": oauth_client.client_id,
            "redirect_uri": callback_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "consent",  # Ensure refresh token
            "resource": nextcloud_resource_uri,  # Request tokens for Nextcloud API access
        }

        auth_url = f"{oauth_client.authorization_endpoint}?{urlencode(idp_params)}"
        logger.debug("Redirecting to external IdP login: %s", auth_url.split("?")[0])
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

        # Fetch authorization endpoint via the shared 5-minute discovery
        # cache (PR #758 nit 5) so each browser login doesn't hit the IdP's
        # discovery endpoint.
        discovery = await get_oidc_discovery(discovery_url)
        authorization_endpoint = discovery["authorization_endpoint"]

        # Include offline_access only if the IdP advertises it (or if
        # scopes_supported is absent from the discovery document).
        # IdPs like AWS Cognito provide refresh tokens automatically without
        # supporting the offline_access scope.
        idp_scopes = discovery.get("scopes_supported")
        if idp_scopes is None or "offline_access" in idp_scopes:
            scopes += " offline_access"

        # Replace internal Docker hostname with public URL
        public_issuer = get_settings().nextcloud_public_issuer_url
        if public_issuer:
            internal_parsed = parse_url(oauth_config["nextcloud_host"])
            auth_parsed = parse_url(authorization_endpoint)

            if auth_parsed.hostname == internal_parsed.hostname:
                public_parsed = parse_url(public_issuer)
                authorization_endpoint = (
                    f"{public_parsed.scheme}://{public_parsed.netloc}{auth_parsed.path}"
                )

        # Get Nextcloud resource URI for audience (background sync needs Nextcloud-scoped tokens)
        nextcloud_resource_uri = oauth_config.get(
            "nextcloud_resource_uri", oauth_config.get("nextcloud_host")
        )

        idp_params = {
            "client_id": oauth_config["client_id"],
            "redirect_uri": callback_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "consent",  # Ensure refresh token
            "resource": nextcloud_resource_uri,  # Request tokens for Nextcloud API access
        }

        logger.debug("Building Nextcloud OIDC auth URL with params: %s", idp_params)

        auth_url = f"{authorization_endpoint}?{urlencode(idp_params)}"
        logger.debug("Redirecting to Nextcloud OIDC login: %s", auth_url)

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
        logger.error("OAuth login error: %s - %s", error, error_description)
        login_url = str(request.url_for("oauth_login"))
        # html_escape: error / error_description come from attacker-controlled
        # query parameters and would otherwise reflect into the failure page.
        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
            <head><title>Login Failed</title></head>
            <body>
                <h1>Login Failed</h1>
                <p>Error: {html_escape(error)}</p>
                <p>{html_escape(error_description)}</p>
                <p><a href="{html_escape(login_url)}">Try again</a></p>
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
    # Same GH #1068 fallback as oauth_login: the callback also writes to storage
    # and must not dereference a None captured under offline-access-off config.
    storage = oauth_ctx.get("storage") or await get_shared_storage()
    oauth_client = oauth_ctx["oauth_client"]
    oauth_config = oauth_ctx["config"]

    # Retrieve code_verifier, nonce, and redirect URL from session storage.
    # Fail closed when the row is missing/expired: otherwise PKCE +
    # nonce verification silently degrade to no-ops (round-6 review).
    oauth_session = await storage.get_oauth_session(state)
    if not oauth_session:
        logger.warning("OAuth callback received unknown/expired state=%s", state[:16])
        return HTMLResponse(
            "Unknown or expired session — please try logging in again.",
            status_code=400,
        )
    # `mcp_authorization_code` field reused to store the PKCE code_verifier
    # (one-time-use). Renaming the column requires a schema migration.
    code_verifier = oauth_session.get("mcp_authorization_code", "")
    # nonce bound to this auth request — verified against the ID token
    # below (PR #758 finding 2).
    nonce = oauth_session.get("nonce")
    # next_url was stored in client_redirect_uri field — re-validate at
    # read-time as defense-in-depth (issue #758 finding 3). The session
    # row could have been written by an older code path or reused.
    next_url = _safe_next_url(oauth_session.get("client_redirect_uri"), "/app")
    # One-time-use session: delete eagerly so a replayed callback can't
    # be processed and so the oauth_sessions table doesn't accumulate
    # completed-but-not-yet-expired browser-login rows.
    await storage.delete_oauth_session(state)

    # Exchange authorization code for tokens
    mcp_server_url = oauth_config["mcp_server_url"]
    callback_uri = f"{mcp_server_url}/oauth/callback"

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

            async with nextcloud_httpx_client() as http_client:
                response = await http_client.post(
                    oauth_client.token_endpoint,
                    data=token_params,
                )
                response.raise_for_status()
                token_data = response.json()
        else:
            # Integrated mode (Nextcloud OIDC)
            discovery_url = oauth_config.get("discovery_url")
            # Use the shared 5-minute discovery cache; oauth_login() above
            # has already populated it for this discovery_url so the
            # callback should hit the cache rather than re-fetching.
            discovery = await get_oidc_discovery(discovery_url)
            token_endpoint = discovery["token_endpoint"]

            token_params = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_uri,
                "client_id": oauth_config["client_id"],
                "client_secret": oauth_config["client_secret"],
            }

            # Add code_verifier for PKCE (required by Nextcloud OIDC)
            if code_verifier:
                token_params["code_verifier"] = code_verifier

            async with nextcloud_httpx_client() as http_client:
                response = await http_client.post(
                    token_endpoint,
                    data=token_params,
                )
                response.raise_for_status()
                token_data = response.json()

    except httpx.HTTPStatusError as e:
        # Correlation IDs let the user reference a specific failure in the
        # server logs without us having to reflect raw exception/IdP text
        # back into the HTML page (PR #758 round-3 nit 6).
        correlation_id = secrets.token_hex(8)
        error_body = (
            e.response.text if hasattr(e.response, "text") else str(e.response.content)
        )
        logger.error(
            "Token exchange failed (correlation_id=%s): HTTP %s - %s",
            correlation_id,
            e.response.status_code,
            error_body,
        )
        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
            <head><title>Login Failed</title></head>
            <body>
                <h1>Login Failed</h1>
                <p>An internal error occurred while exchanging the authorization code.</p>
                <p>Correlation ID: <code>{html_escape(correlation_id)}</code></p>
                <p>Please try again, or contact your administrator if the problem persists.</p>
            </body>
            </html>
            """,
            status_code=500,
        )
    except Exception as e:
        correlation_id = secrets.token_hex(8)
        logger.error("Token exchange failed (correlation_id=%s): %s", correlation_id, e)
        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
            <head><title>Login Failed</title></head>
            <body>
                <h1>Login Failed</h1>
                <p>An internal error occurred while exchanging the authorization code.</p>
                <p>Correlation ID: <code>{html_escape(correlation_id)}</code></p>
                <p>Please try again, or contact your administrator if the problem persists.</p>
            </body>
            </html>
            """,
            status_code=500,
        )

    refresh_token = token_data.get("refresh_token")
    id_token = token_data.get("id_token")

    # Demoted to DEBUG (PR #758 nit a) — these were previously logged at
    # INFO on every login.
    logger.debug("Token exchange response keys: %s", token_data.keys())
    logger.debug("Refresh token present: %s", refresh_token is not None)
    logger.debug("ID token present: %s", id_token is not None)

    # Resolve the discovery URL + audience used for THIS auth request so
    # we can verify the ID token signature + claims (issue #626 finding 1).
    if oauth_client:
        # External IdP path
        verification_audience = oauth_client.client_id
        verification_discovery_url = getattr(oauth_client, "discovery_url", None)
    else:
        # Integrated Nextcloud OIDC path
        verification_audience = oauth_config["client_id"]
        verification_discovery_url = oauth_config.get("discovery_url")

    if not verification_discovery_url:
        logger.error("Cannot verify ID token: no discovery_url available")
        return HTMLResponse(
            "<h1>Login Failed</h1><p>OIDC discovery URL not configured</p>",
            status_code=500,
        )

    try:
        userinfo = await verify_id_token(
            id_token,
            discovery_url=verification_discovery_url,
            expected_audience=verification_audience,
            expected_nonce=nonce,
        )
    except IdTokenVerificationError as e:
        # Same correlation-ID pattern as token-exchange failures
        # (PR #758 round-3 nit 6) — log the detail server-side and only
        # show a generic message + correlation ID in the browser.
        correlation_id = secrets.token_hex(8)
        logger.error(
            "ID token verification failed (correlation_id=%s): %s",
            correlation_id,
            e,
        )
        return HTMLResponse(
            f"<h1>Login Failed</h1>"
            f"<p>The ID token failed verification.</p>"
            f"<p>Correlation ID: <code>{html_escape(correlation_id)}</code></p>",
            status_code=400,
        )

    user_id = userinfo["sub"]
    username = userinfo.get("preferred_username") or userinfo.get("email")
    logger.info("Browser login successful: %s (sub=%s)", username, user_id)

    # Calculate refresh token expiration from token response
    refresh_expires_in = token_data.get("refresh_expires_in")
    refresh_expires_at = None
    if refresh_expires_in:
        # Some IdPs (e.g. AWS Cognito) return refresh_expires_in as a JSON
        # string rather than an int; coerce to be safe.
        refresh_expires_at = int(time.time()) + int(refresh_expires_in)
        logger.debug(
            "Refresh token expires in %ss (at timestamp %s)",
            refresh_expires_in,
            refresh_expires_at,
        )

    # Extract granted scopes
    granted_scopes = (
        token_data.get("scope", "").split() if token_data.get("scope") else None
    )

    # Store refresh token (for background jobs ONLY). The browser session
    # itself is gated on this — without a refresh token, ``SessionAuthBackend``
    # would reject every subsequent request and silently bounce the user back
    # to ``/oauth/login`` (PR #758 round-7 medium 1).
    if not refresh_token:
        correlation_id = secrets.token_urlsafe(8)
        logger.error(
            "No refresh token in token response — cannot establish browser "
            "session (correlation_id=%s, user_id=%s)",
            correlation_id,
            user_id,
        )
        return HTMLResponse(
            f"<h1>Login Failed</h1>"
            f"<p>The identity provider did not return a refresh token, so a "
            f"persistent session could not be established. Make sure "
            f"<code>offline_access</code> is granted in the IdP configuration.</p>"
            f"<p>Correlation ID: <code>{html_escape(correlation_id)}</code></p>",
            status_code=400,
        )

    logger.debug(
        "Storing refresh token for user_id=%s state=%s... scopes=%s expires_at=%s",
        user_id,
        state[:16],
        granted_scopes,
        refresh_expires_at,
    )
    await storage.store_refresh_token(
        user_id=user_id,
        refresh_token=refresh_token,
        expires_at=refresh_expires_at,
        flow_type="browser",  # Browser-based login flow
        provisioning_client_id=state,  # Store state for unified session lookup
        scopes=granted_scopes,
    )
    logger.info(
        "Refresh token stored for user %s (lookup key: %s...)",
        user_id,
        state[:16],
    )

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
                    logger.debug("User profile cached for %s", user_id)
                else:
                    logger.warning("Failed to query userinfo endpoint for %s", user_id)
            else:
                logger.warning("Could not determine userinfo endpoint")
        except Exception as e:
            logger.error("Error caching user profile: %s", e)
            # Continue anyway - profile cache is optional for browser UI

    # Create a server-side browser session: a random opaque session_id is
    # mapped to the verified user_id in `browser_sessions`. The cookie value
    # is the session_id (never the raw user_id — see issue #626 finding 2).
    session_id = secrets.token_urlsafe(32)
    session_ttl = 86400 * 30  # 30 days
    await storage.create_browser_session(
        session_id=session_id, user_id=user_id, ttl_seconds=session_ttl
    )

    response = RedirectResponse(next_url, status_code=302)
    # CSRF protection is layered: ``SameSite=Lax`` blocks cross-site POSTs
    # in modern browsers; ``oauth_logout`` is POST-only with an Origin /
    # Referer check (``_origin_matches_self``) to cover older browsers and
    # non-browser clients. ``HttpOnly`` blocks JS exfiltration on XSS;
    # ``Secure`` is gated to non-HTTP hosts in dev (PR #758 round-4 review
    # nit 6).
    response.set_cookie(
        key="mcp_session",
        value=session_id,
        max_age=session_ttl,
        httponly=True,
        secure=_should_use_secure_cookies(),
        samesite="lax",
    )

    logger.info("Session cookie set for user %s (sid=%s…)", username, session_id[:8])
    return response


async def oauth_logout(request: Request) -> RedirectResponse | JSONResponse:
    """Browser OAuth logout — invalidate session and revoke refresh token.

    Issue #626 finding 4: prior implementation only cleared the cookie,
    leaving the refresh token in storage (valid up to 90 days). This now:
      1. Resolves the user_id for the current browser session_id.
      2. Calls the IdP `revocation_endpoint` for the stored refresh token
         when the IdP advertises one.
      3. Deletes the stored refresh token regardless of revocation success.
      4. Deletes the browser_sessions row so the cookie is unusable even
         if it leaks.
      5. Clears the cookie on the response.

    Method is POST-only at the route layer to defeat passive CSRF (PR #758
    round-3 review hardening). Origin / Referer headers are also validated
    against the configured ``mcp_server_url`` when present, blocking
    same-method-but-cross-origin form submissions.

    Query parameters:
        next: Optional URL to redirect to after logout (default: /oauth/login)
    """
    next_url = _safe_next_url(request.query_params.get("next"), "/oauth/login")
    session_id = request.cookies.get("mcp_session")

    oauth_ctx = getattr(request.app.state, "oauth_context", None)

    # CSRF check: when Origin or Referer is present, host must match the
    # MCP server's own host. Per OWASP CSRF cheat sheet, we allow the
    # request through when neither header is present (some user agents
    # strip both for privacy on same-origin POST).
    if oauth_ctx and not _origin_matches_self(request, oauth_ctx):
        logger.warning(
            "Logout blocked: cross-origin request from %s",
            request.headers.get("origin") or request.headers.get("referer"),
        )
        return JSONResponse({"error": "forbidden"}, status_code=403)

    storage = oauth_ctx.get("storage") if oauth_ctx else None

    if session_id and storage and oauth_ctx:
        try:
            user_id = await storage.get_browser_session_user(session_id)
            if user_id:
                token_data = await storage.get_refresh_token(user_id)
                refresh_token = token_data.get("refresh_token") if token_data else None

                if refresh_token:
                    await _revoke_refresh_token_at_idp(oauth_ctx, refresh_token)
                    await storage.delete_refresh_token(user_id)
                    logger.info("Refresh token revoked + deleted for user %s", user_id)
        except Exception as e:
            # Logout must always succeed locally; log and continue.
            logger.warning("Logout cleanup failed (continuing): %s", e)
        finally:
            # Always drop the browser_sessions row, even when the
            # refresh-token cleanup above failed — otherwise an orphan
            # row lingers until the hourly cleanup cron (PR #758 round-5
            # review medium 1). Not exploitable (SessionAuthBackend
            # already rejects sessions without a live refresh token), but
            # a correctness gap worth closing here.
            try:
                await storage.delete_browser_session(session_id)
            except Exception as e:
                logger.warning(
                    "Failed to delete browser session %s…: %s", session_id[:8], e
                )

    response = RedirectResponse(next_url, status_code=302)
    # Match the attributes from set_cookie so browsers reliably evict the
    # cookie even on edge-case implementations that consider security flags
    # when matching for deletion.
    response.delete_cookie(
        "mcp_session",
        httponly=True,
        secure=_should_use_secure_cookies(),
        samesite="lax",
    )

    logger.info("User logged out, session cookie cleared")
    return response


async def _revoke_refresh_token_at_idp(oauth_ctx: dict, refresh_token: str) -> None:
    """Best-effort RFC 7009 revocation against the IdP.

    Silent on failure: revoking remotely is a defense-in-depth step on top
    of deleting the local copy, and we don't want logout to error if the
    IdP is unreachable or doesn't advertise a revocation endpoint.
    """
    # Production oauth_context nests config under "config" (see app.py
    # starlette_lifespan). A flat shape is also accepted for tests and
    # historical callers.
    cfg = oauth_ctx.get("config") or oauth_ctx
    settings = get_settings()
    try:
        discovery_url = cfg.get("discovery_url") or settings.oidc_discovery_url
        if not discovery_url and settings.nextcloud_host:
            # Strip trailing slash so a host configured as
            # ``https://cloud.example.com/`` doesn't produce a double-slash
            # in the well-known URL (PR #758 round-4 review nit 5).
            discovery_url = (
                f"{settings.nextcloud_host.rstrip('/')}"
                "/.well-known/openid-configuration"
            )
        if not discovery_url:
            return

        # Re-use the shared 5-minute discovery cache (PR #758 nit 6) so a
        # burst of logouts doesn't hammer the IdP's discovery endpoint.
        discovery = await get_oidc_discovery(discovery_url)
        revocation_endpoint = discovery.get("revocation_endpoint")
        if not revocation_endpoint:
            logger.debug("IdP advertises no revocation_endpoint; skipping")
            return

        client_id = cfg.get("client_id") or settings.oidc_client_id
        client_secret = cfg.get("client_secret") or settings.oidc_client_secret
        if not (client_id and client_secret):
            logger.debug("No OIDC client credentials available for revocation")
            return

        async with nextcloud_httpx_client() as http_client:
            response = await http_client.post(
                revocation_endpoint,
                data={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                },
                auth=(client_id, client_secret),
            )
            if response.status_code >= 400:
                logger.warning(
                    "Refresh token revocation returned HTTP %s", response.status_code
                )
    except Exception as e:
        logger.warning("Refresh token revocation failed: %s", e)
