"""Web-based Login Flow v2 provisioning routes.

Provides browser endpoints for provisioning Nextcloud app passwords via
Login Flow v2. Used by Astrolabe's "Enable Semantic Search" flow to
chain OAuth (bearer token) with Login Flow v2 (app password) in a single
user interaction.

Flow:
1. GET /app/provision?redirect_uri=...  → Initiates LFv2, redirects to NC login
2. User clicks "Grant access" on Nextcloud's login page
3. MCP server background task polls and stores app password
4. GET /app/provision/status?id=... → Returns completion status (JSON)
5. User returns to Astrolabe settings (via redirect_uri or navigation)
"""

import html
import logging
import os
import secrets
import time
from urllib.parse import urlparse

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from nextcloud_mcp_server.auth.login_flow import LoginFlowV2Client, rewrite_url_origin
from nextcloud_mcp_server.auth.storage import get_shared_storage
from nextcloud_mcp_server.config import get_nextcloud_ssl_verify, get_settings

logger = logging.getLogger(__name__)

# In-memory store for web provision sessions (short-lived, no persistence needed)
# Maps provision_id → session data
_provision_sessions: dict[str, dict] = {}

# Session TTL: 20 minutes (matches Nextcloud's Login Flow v2 timeout)
_SESSION_TTL = 1200


def _cleanup_expired_sessions() -> None:
    """Remove expired provision sessions."""
    now = time.time()
    expired = [k for k, v in _provision_sessions.items() if v["expires_at"] < now]
    for k in expired:
        del _provision_sessions[k]


def _validate_redirect_uri(redirect_uri: str) -> bool:
    """Validate that redirect_uri is a reasonable URL (not javascript: etc)."""
    try:
        parsed = urlparse(redirect_uri)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


async def _poll_and_store(provision_id: str) -> None:
    """Background task: poll Login Flow v2 and store app password on completion."""
    session = _provision_sessions.get(provision_id)
    if not session:
        return

    settings = get_settings()
    nextcloud_host = settings.nextcloud_host
    if not nextcloud_host:
        if provision_id in _provision_sessions:
            session["status"] = "error"
        return

    flow_client = LoginFlowV2Client(
        nextcloud_host=nextcloud_host,
        verify_ssl=get_nextcloud_ssl_verify(),
    )

    poll_endpoint = session["poll_endpoint"]
    poll_token = session["poll_token"]
    user_id = session.get("user_id")

    # Poll every 2 seconds for up to 20 minutes
    max_attempts = 600
    for _ in range(max_attempts):
        if provision_id not in _provision_sessions:
            return  # Session was cleaned up

        try:
            result = await flow_client.poll(poll_endpoint, poll_token)
        except Exception as e:
            logger.warning(
                f"Login Flow v2 poll error for provision {provision_id}: {e}"
            )
            await anyio.sleep(2)
            continue

        if result.status == "completed":
            # Store the app password
            storage = await get_shared_storage()
            effective_user_id = user_id or result.login_name or "unknown"
            if not result.app_password:
                # Re-fetch session to avoid writing to orphaned dict if
                # _cleanup_expired_sessions removed it while we were polling
                session = _provision_sessions.get(provision_id)
                if session:
                    session["status"] = "error"
                logger.error(
                    f"Login Flow v2 completed but no app_password (provision_id={provision_id})"
                )
                return
            await storage.store_app_password_with_scopes(
                user_id=effective_user_id,
                app_password=result.app_password,
                scopes=None,  # All scopes
                username=result.login_name,
            )
            session = _provision_sessions.get(provision_id)
            if session:
                session["status"] = "completed"
                session["username"] = result.login_name
            logger.info(
                f"Login Flow v2 web provision completed for user {effective_user_id} "
                f"(provision_id={provision_id})"
            )
            return

        if result.status == "expired":
            session = _provision_sessions.get(provision_id)
            if session:
                session["status"] = "expired"
            logger.warning(
                f"Login Flow v2 web provision expired (provision_id={provision_id})"
            )
            return

        await anyio.sleep(2)

    # Timed out
    session = _provision_sessions.get(provision_id)
    if session:
        session["status"] = "expired"
    logger.warning(
        f"Login Flow v2 web provision timed out (provision_id={provision_id})"
    )


async def provision_page(request: Request) -> RedirectResponse | HTMLResponse:
    """Initiate Login Flow v2 and redirect to Nextcloud's login page.

    GET /app/provision?redirect_uri=...&user_id=...

    Initiates Login Flow v2, starts background polling, and redirects the
    browser to Nextcloud's login/grant page. After the user grants access,
    the background task stores the app password. The user then navigates
    back to the redirect_uri (Astrolabe settings).
    """
    _cleanup_expired_sessions()

    redirect_uri = request.query_params.get("redirect_uri", "")
    user_id = request.query_params.get("user_id", "")

    if not redirect_uri or not _validate_redirect_uri(redirect_uri):
        return HTMLResponse(
            content=_render_error("Missing or invalid redirect_uri parameter."),
            status_code=400,
        )

    if urlparse(redirect_uri).scheme == "http":
        logger.warning(f"Provision redirect_uri uses insecure HTTP: {redirect_uri}")

    # Check if user already has an app password — skip straight to redirect
    if user_id:
        storage = await get_shared_storage()
        existing = await storage.get_app_password_with_scopes(user_id)
        if existing:
            logger.info(f"User {user_id} already has app password, skipping provision")
            return RedirectResponse(redirect_uri)

    # Initiate Login Flow v2
    settings = get_settings()
    nextcloud_host = settings.nextcloud_host
    if not nextcloud_host:
        return HTMLResponse(
            content=_render_error("Nextcloud host not configured on server."),
            status_code=500,
        )

    try:
        flow_client = LoginFlowV2Client(
            nextcloud_host=nextcloud_host,
            verify_ssl=get_nextcloud_ssl_verify(),
        )
        init_response = await flow_client.initiate()
    except Exception as e:
        logger.error(f"Failed to initiate Login Flow v2 for web provision: {e}")
        return HTMLResponse(
            content=_render_error(f"Failed to start login flow: {e}"),
            status_code=502,
        )

    # Create provision session
    provision_id = secrets.token_urlsafe(32)
    _provision_sessions[provision_id] = {
        "status": "pending",
        "login_url": init_response.login_url,
        "poll_endpoint": init_response.poll_endpoint,
        "poll_token": init_response.poll_token,
        "redirect_uri": redirect_uri,
        "user_id": user_id,
        "created_at": time.time(),
        "expires_at": time.time() + _SESSION_TTL,
    }

    # Start background polling task (uses task group from app lifespan)
    poll_tg = getattr(request.app.state, "poll_task_group", None)
    if poll_tg is None:
        logger.error("No poll task group available; cannot start background polling")
        _provision_sessions.pop(provision_id, None)
        return HTMLResponse(
            content=_render_error(
                "Server configuration error: background polling unavailable."
            ),
            status_code=500,
        )
    poll_tg.start_soon(_poll_and_store, provision_id)

    logger.info(
        f"Login Flow v2 web provision initiated (provision_id={provision_id}, "
        f"user_id={user_id or 'unknown'}), redirecting to NC login"
    )

    # Redirect to Nextcloud's Login Flow v2 login page.
    # The login_url may use the internal Docker hostname (http://app/...).
    # Replace with the public Nextcloud URL for the browser.
    login_url = init_response.login_url
    public_issuer = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL", "")
    if public_issuer and nextcloud_host:
        login_url = rewrite_url_origin(login_url, public_issuer.rstrip("/"))

    return RedirectResponse(login_url)


async def provision_status(request: Request) -> JSONResponse:
    """Check provision session status.

    GET /app/provision/status?id=...

    Returns JSON with status field:
    - ``"pending"``  — flow in progress, poll again
    - ``"completed"`` — app password stored, includes ``"username"``
    - ``"expired"``  — flow timed out or was rejected by Nextcloud
    - ``"error"``    — flow completed but server-side error (e.g. missing app password)
    - ``"not_found"`` — unknown or already-consumed session (404)
    """
    provision_id = request.query_params.get("id", "")

    session = _provision_sessions.get(provision_id)
    if not session:
        return JSONResponse(
            {
                "status": "not_found",
                "message": "Provision session not found or expired",
            },
            status_code=404,
        )

    # Detect sessions that outlived their TTL (e.g. no new provision
    # requests triggered _cleanup_expired_sessions)
    if session["expires_at"] < time.time():
        _provision_sessions.pop(provision_id, None)
        return JSONResponse({"status": "expired"}, status_code=404)

    response: dict = {"status": session["status"]}
    if session["status"] == "completed":
        response["username"] = session.get("username")
        # Clean up completed session after status is read
        _provision_sessions.pop(provision_id, None)

    return JSONResponse(response)


# ── HTML rendering helpers ────────────────────────────────────────────────


def _render_error(message: str) -> str:
    """Render an error page."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Error - Astrolabe</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
        }}
        .card {{
            background: #fff;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            padding: 2.5rem;
            max-width: 480px;
            text-align: center;
        }}
        .error {{ color: #c62828; }}
    </style>
</head>
<body>
    <div class="card">
        <h1 class="error">Provisioning Error</h1>
        <p>{html.escape(message)}</p>
    </div>
</body>
</html>"""
