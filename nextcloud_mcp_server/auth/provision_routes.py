"""Web-based Login Flow v2 provisioning routes.

Provides browser endpoints for provisioning Nextcloud app passwords via
Login Flow v2. Used by Astrolabe's "Enable Semantic Search" flow to
chain OAuth (bearer token) with Login Flow v2 (app password) in a single
user interaction.

Flow:
1. GET /app/provision?redirect_uri=...  → Initiates LFv2, renders polling page
2. The page opens Nextcloud's login URL in a popup window
3. User clicks "Grant access" in the popup
4. Page polls GET /app/provision/status?id=... for completion
5. On success, redirects to redirect_uri
"""

import asyncio
import logging
import secrets
import time
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from nextcloud_mcp_server.auth.login_flow import LoginFlowV2Client
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
        session["status"] = "expired"
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
            await asyncio.sleep(2)
            continue

        if result.status == "completed":
            # Store the app password
            storage = await get_shared_storage()
            effective_user_id = user_id or result.login_name or "unknown"
            if not result.app_password:
                session["status"] = "expired"
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
            session["status"] = "completed"
            session["username"] = result.login_name
            logger.info(
                f"Login Flow v2 web provision completed for user {effective_user_id} "
                f"(provision_id={provision_id})"
            )
            return

        if result.status == "expired":
            session["status"] = "expired"
            logger.warning(
                f"Login Flow v2 web provision expired (provision_id={provision_id})"
            )
            return

        await asyncio.sleep(2)

    # Timed out
    session["status"] = "expired"
    logger.warning(
        f"Login Flow v2 web provision timed out (provision_id={provision_id})"
    )


async def provision_page(request: Request) -> HTMLResponse:
    """Render the Login Flow v2 provisioning page.

    GET /app/provision?redirect_uri=...&user_id=...

    Initiates Login Flow v2, starts background polling, and returns an HTML
    page that opens Nextcloud's login URL in a popup and polls for completion.
    """
    _cleanup_expired_sessions()

    redirect_uri = request.query_params.get("redirect_uri", "")
    user_id = request.query_params.get("user_id", "")

    if not redirect_uri or not _validate_redirect_uri(redirect_uri):
        return HTMLResponse(
            content=_render_error("Missing or invalid redirect_uri parameter."),
            status_code=400,
        )

    # Check if user already has an app password
    if user_id:
        storage = await get_shared_storage()
        existing = await storage.get_app_password_with_scopes(user_id)
        if existing:
            logger.info(f"User {user_id} already has app password, skipping provision")
            return HTMLResponse(
                content=_render_redirect(redirect_uri),
                status_code=200,
            )

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
        init_response = await flow_client.initiate(
            user_agent="Astrolabe Background Sync"
        )
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

    # Start background polling task
    asyncio.create_task(_poll_and_store(provision_id))

    logger.info(
        f"Login Flow v2 web provision initiated (provision_id={provision_id}, "
        f"user_id={user_id or 'unknown'})"
    )

    return HTMLResponse(
        content=_render_provision_page(
            provision_id=provision_id,
            login_url=init_response.login_url,
            redirect_uri=redirect_uri,
        )
    )


async def provision_status(request: Request) -> JSONResponse:
    """Check provision session status.

    GET /app/provision/status?id=...

    Returns JSON: {"status": "pending"|"completed"|"expired", "username": "..."}
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

    response: dict = {"status": session["status"]}
    if session["status"] == "completed":
        response["username"] = session.get("username")
        # Clean up completed session after status is read
        _provision_sessions.pop(provision_id, None)

    return JSONResponse(response)


# ── HTML rendering helpers ────────────────────────────────────────────────


def _render_provision_page(provision_id: str, login_url: str, redirect_uri: str) -> str:
    """Render the provisioning page HTML."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connecting to Nextcloud - Astrolabe</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Open Sans', sans-serif;
            background: #f5f5f5;
            color: #222;
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
            width: 90%;
            text-align: center;
        }}
        h1 {{
            font-size: 1.4rem;
            margin-bottom: 1rem;
            color: #00679e;
        }}
        .status {{
            margin: 1.5rem 0;
            padding: 1rem;
            border-radius: 8px;
            background: #e5eff5;
        }}
        .status.error {{
            background: #fde8e8;
            color: #c62828;
        }}
        .status.success {{
            background: #e8f5e9;
            color: #2e7d32;
        }}
        .spinner {{
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid #e5eff5;
            border-top-color: #00679e;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            vertical-align: middle;
            margin-right: 8px;
        }}
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
        .btn {{
            display: inline-block;
            padding: 10px 24px;
            background: #00679e;
            color: #fff;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 1rem;
            text-decoration: none;
            margin-top: 1rem;
        }}
        .btn:hover {{ background: #005580; }}
        .btn:disabled {{
            background: #ccc;
            cursor: not-allowed;
        }}
        .help {{
            margin-top: 1.5rem;
            font-size: 0.85rem;
            color: #6b6b6b;
        }}
        #popup-blocked {{
            display: none;
            margin-top: 1rem;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Connect to Nextcloud</h1>
        <p>Grant Astrolabe access to your Nextcloud account for background sync.</p>

        <div id="status" class="status">
            <span class="spinner"></span>
            Waiting for authorization...
        </div>

        <div id="popup-blocked">
            <p>Could not open the login window automatically.</p>
            <a class="btn" href="{login_url}" target="_blank" rel="noopener"
               id="manual-open-btn">Open Login Page</a>
        </div>

        <div id="grant-hint" class="help">
            A popup window should open. Click <strong>"Grant access"</strong> in the
            Nextcloud window to continue.
        </div>
    </div>

    <script>
        (function() {{
            const provisionId = "{provision_id}";
            const redirectUri = "{redirect_uri}";
            const loginUrl = "{login_url}";
            let popup = null;
            let pollInterval = null;

            // Try to open popup
            try {{
                popup = window.open(loginUrl, "nextcloud_login",
                    "width=600,height=700,scrollbars=yes,resizable=yes");
            }} catch(e) {{
                // Popup blocked
            }}

            if (!popup || popup.closed) {{
                document.getElementById("popup-blocked").style.display = "block";
                document.getElementById("grant-hint").textContent =
                    "After granting access, this page will update automatically.";
            }}

            // Poll for completion
            function checkStatus() {{
                fetch("/app/provision/status?id=" + encodeURIComponent(provisionId))
                    .then(r => r.json())
                    .then(data => {{
                        if (data.status === "completed") {{
                            clearInterval(pollInterval);
                            if (popup && !popup.closed) popup.close();
                            const el = document.getElementById("status");
                            el.className = "status success";
                            el.innerHTML = "✓ Connected as <strong>" +
                                (data.username || "user") + "</strong>. Redirecting...";
                            setTimeout(() => {{ window.location.href = redirectUri; }}, 1500);
                        }} else if (data.status === "expired" || data.status === "not_found") {{
                            clearInterval(pollInterval);
                            if (popup && !popup.closed) popup.close();
                            const el = document.getElementById("status");
                            el.className = "status error";
                            el.innerHTML = "Authorization expired. Please try again.";
                        }}
                    }})
                    .catch(() => {{
                        // Network error, keep polling
                    }});
            }}

            pollInterval = setInterval(checkStatus, 2000);

            // Also check immediately after a short delay
            setTimeout(checkStatus, 1000);
        }})();
    </script>
</body>
</html>"""


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
        <p>{message}</p>
    </div>
</body>
</html>"""


def _render_redirect(redirect_uri: str) -> str:
    """Render a page that immediately redirects (for already-provisioned users)."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="0;url={redirect_uri}">
    <title>Redirecting...</title>
</head>
<body>
    <p>Already connected. Redirecting...</p>
    <script>window.location.href = "{redirect_uri}";</script>
</body>
</html>"""
