"""App password management API endpoints.

Provides REST API endpoints for app password provisioning in multi-user BasicAuth mode.
These endpoints are used by the Nextcloud PHP app (Astrolabe) to:
- Store app passwords for background sync operations
- Check app password status
- Delete stored app passwords

Authentication is via BasicAuth with the user's Nextcloud credentials.
Passwords are validated against Nextcloud before being stored.
"""

import base64
import logging
import re
import time
from collections import defaultdict

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.api.management import _sanitize_error_for_client
from nextcloud_mcp_server.auth.scope_authorization import invalidate_scope_cache
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.config import get_settings

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)

# App password format regex (Nextcloud format: xxxxx-xxxxx-xxxxx-xxxxx-xxxxx)
# Shape guard only — the authoritative check is the BasicAuth validation
# against Nextcloud below. Accepts both the dashed format a user copies from
# Security settings (xxxxx-xxxxx-xxxxx-xxxxx-xxxxx) and the raw token returned
# by the one-click ``core/getapppassword`` flow (a long alphanumeric string).
APP_PASSWORD_PATTERN = re.compile(r"^[a-zA-Z0-9-]{20,256}$")

# Timeout for Nextcloud API validation requests (seconds)
NEXTCLOUD_VALIDATION_TIMEOUT = 10.0

# OCS meta status codes that indicate success. OCS v1 (``/ocs/v1.php``) reports
# 100; OCS v2 (``/ocs/v2.php``) reports 200. We query v2 (see
# ``_validate_nextcloud_credentials``) but accept both for robustness.
_OCS_SUCCESS_STATUSCODES = frozenset({100, 200})

# Rate limiting configuration for app password provisioning
# Limits: 5 attempts per user per hour
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 3600  # 1 hour

# In-memory rate limiter storage
# Structure: {user_id: [(timestamp, success), ...]}
_rate_limit_attempts: dict[str, list[tuple[float, bool]]] = defaultdict(list)


def _check_rate_limit(user_id: str) -> tuple[bool, int]:
    """Check if user is rate limited for app password operations.

    Implements a sliding window rate limiter to prevent brute-force attacks
    on the app password provisioning endpoint.

    Args:
        user_id: User identifier to check

    Returns:
        Tuple of (is_allowed, seconds_until_retry)
        - is_allowed: True if request should be allowed
        - seconds_until_retry: Seconds to wait if rate limited (0 if allowed)
    """
    current_time = time.time()
    window_start = current_time - RATE_LIMIT_WINDOW_SECONDS

    # Clean up old attempts outside the window
    _rate_limit_attempts[user_id] = [
        (ts, success)
        for ts, success in _rate_limit_attempts[user_id]
        if ts > window_start
    ]

    # Count recent attempts (both successful and failed)
    recent_attempts = len(_rate_limit_attempts[user_id])

    if recent_attempts >= RATE_LIMIT_MAX_ATTEMPTS:
        # Find when the oldest attempt in the window will expire
        oldest_attempt = min(ts for ts, _ in _rate_limit_attempts[user_id])
        seconds_until_retry = int(
            oldest_attempt + RATE_LIMIT_WINDOW_SECONDS - current_time
        )
        return False, max(1, seconds_until_retry)

    return True, 0


def _record_rate_limit_attempt(user_id: str, success: bool) -> None:
    """Record an app password provisioning attempt for rate limiting.

    Args:
        user_id: User identifier
        success: Whether the attempt was successful
    """
    _rate_limit_attempts[user_id].append((time.time(), success))


def _extract_basic_auth(
    request: Request, path_user_id: str
) -> tuple[str, str, JSONResponse | None]:
    """Extract and validate BasicAuth credentials from request.

    Validates:
    1. Authorization header is present and valid BasicAuth format
    2. Username in credentials matches the path user_id

    Args:
        request: Starlette request with Authorization header
        path_user_id: User ID from the URL path to verify against

    Returns:
        Tuple of (username, password, error_response)
        - If successful: (username, password, None)
        - If failed: ("", "", JSONResponse with error)
    """
    auth_header = request.headers.get("Authorization")

    if not auth_header or not auth_header.startswith("Basic "):
        return (
            "",
            "",
            JSONResponse(
                {"success": False, "error": "Missing BasicAuth credentials"},
                status_code=401,
            ),
        )

    try:
        # Decode BasicAuth
        encoded = auth_header.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return (
            "",
            "",
            JSONResponse(
                {"success": False, "error": "Invalid BasicAuth format"},
                status_code=401,
            ),
        )

    # Verify username matches path user_id
    if username != path_user_id:
        logger.warning(
            "Username mismatch in app password operation for path user %s", path_user_id
        )
        return (
            "",
            "",
            JSONResponse(
                {"success": False, "error": "Username does not match path user_id"},
                status_code=403,
            ),
        )

    return username, password, None


async def _get_app_password_storage(request: Request) -> RefreshTokenStorage:
    """Get or initialize RefreshTokenStorage for app password operations.

    Checks app.state.storage first, then falls back to creating from environment.
    This helper avoids repeated storage initialization logic across endpoints.

    Args:
        request: Starlette request with app state

    Returns:
        Initialized RefreshTokenStorage instance
    """
    storage = getattr(request.app.state, "storage", None)

    if not storage:
        # Multi-user BasicAuth mode may not have oauth_context
        # Initialize storage from environment
        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

    return storage


async def _validate_nextcloud_credentials(
    nextcloud_host: str,
    login_name: str,
    password: str,
    *,
    invalid_credential_error: str = "Invalid app password",
) -> tuple[str | None, JSONResponse | None]:
    """Validate a credential against Nextcloud and return the account UID.

    Authenticates against the OCS ``/cloud/user`` endpoint as ``login_name``.
    Nextcloud keys app-password auth on the *loginName*, which may differ from
    the UID (e.g. OIDC-provisioned users, or the ``admin`` account whose display
    name is ``Admin``).

    Queries OCS **v2** (``/ocs/v2.php``) deliberately. OCS **v1**
    (``/ocs/v1.php``) always returns HTTP 200 — even on auth failure, where it
    wraps the real status in ``ocs.meta.statuscode`` (997 = unauthenticated) and
    returns ``ocs.data`` as an empty list ``[]``. v2 maps the OCS status onto
    the HTTP status, so a failed credential is a real 401. The payload is also
    parsed defensively so a non-dict ``ocs.data`` can never raise — this is the
    crash behind issue #824 (``AttributeError: 'list' object has no attribute
    'get'`` escaping as an unhandled 500).

    Args:
        invalid_credential_error: client-facing error string for the 401 path,
            so callers can keep their own wording.

    Returns:
        ``(ocs_user_id, None)`` on success, otherwise ``(None, error_response)``
        with a ready-to-return :class:`JSONResponse`: **401** for an invalid
        credential, **502** when Nextcloud is unreachable, errors out (5xx /
        maintenance mode), or returns something we cannot parse.
    """
    try:
        async with nextcloud_httpx_client(
            timeout=NEXTCLOUD_VALIDATION_TIMEOUT
        ) as client:
            response = await client.get(
                f"{nextcloud_host}/ocs/v2.php/cloud/user",
                auth=(login_name, password),
                params={"format": "json"},
                headers={"OCS-APIRequest": "true"},
            )
    except httpx.RequestError as e:
        logger.error("Failed to reach Nextcloud for credential validation: %s", e)
        return None, JSONResponse(
            {"success": False, "error": "Failed to validate credentials"},
            status_code=502,
        )

    # v2.php maps an OCS auth failure onto a real HTTP status. Only 401/403 mean
    # "bad credential" — anything else non-200 (5xx, 503 maintenance mode) is a
    # Nextcloud-side problem and must surface as 502, not a misleading "invalid
    # password" that sends ops chasing the wrong cause.
    if response.status_code in (401, 403):
        logger.warning("Credential validation failed: HTTP %s", response.status_code)
        return None, JSONResponse(
            {"success": False, "error": invalid_credential_error},
            status_code=401,
        )
    if response.status_code != 200:
        logger.error("Nextcloud OCS returned HTTP %s", response.status_code)
        return None, JSONResponse(
            {"success": False, "error": "Nextcloud returned a server error"},
            status_code=502,
        )

    # Parse defensively: even on HTTP 200 the body may be malformed or carry a
    # non-dict ``ocs.data`` (the v1.php auth-failure shape, kept as a guard
    # against surprises). Never call ``.get`` on something that isn't a dict.
    try:
        payload = response.json()
    except ValueError as e:
        logger.error("Nextcloud returned a non-JSON OCS response: %s", e)
        return None, JSONResponse(
            {"success": False, "error": "Unexpected response from Nextcloud"},
            status_code=502,
        )

    ocs = payload.get("ocs") if isinstance(payload, dict) else None
    meta = ocs.get("meta") if isinstance(ocs, dict) else None
    statuscode = meta.get("statuscode") if isinstance(meta, dict) else None
    ocs_data = ocs.get("data") if isinstance(ocs, dict) else None
    ocs_user_id = ocs_data.get("id") if isinstance(ocs_data, dict) else None

    # Treat a non-success OCS status, or a payload we can't read a user id from,
    # as a failed validation rather than crashing. ``statuscode`` is ``None``
    # when ``meta`` is absent; fall back to "did we get a user id?" so a minimal
    # but valid response still passes.
    if (
        statuscode is not None and statuscode not in _OCS_SUCCESS_STATUSCODES
    ) or not ocs_user_id:
        logger.warning(
            "Credential validation failed: OCS statuscode=%s, user_id present=%s",
            statuscode,
            ocs_user_id is not None,
        )
        return None, JSONResponse(
            {"success": False, "error": invalid_credential_error},
            status_code=401,
        )

    return ocs_user_id, None


async def provision_app_password(request: Request) -> JSONResponse:
    """POST /api/v1/users/{user_id}/app-password - Store app password for background sync.

    This endpoint is used by Astrolabe (Nextcloud PHP app) to provision app passwords
    for multi-user BasicAuth mode background sync.

    The request must include BasicAuth credentials where:
    - username: Nextcloud user ID (must match path user_id)
    - password: The app password being provisioned

    The MCP server validates the app password against Nextcloud before storing it.
    This proves the user owns the password and has access to Nextcloud.

    Security model:
    - User identity is verified via BasicAuth against Nextcloud
    - App password is encrypted before storage
    - Only the user who owns the password can provision it
    - Rate limited to prevent brute-force attacks
    """
    # Get user_id from path
    path_user_id = request.path_params.get("user_id")
    if not path_user_id:
        return JSONResponse(
            {"success": False, "error": "Missing user_id in path"},
            status_code=400,
        )

    # Check rate limit before processing
    is_allowed, retry_after = _check_rate_limit(path_user_id)
    if not is_allowed:
        logger.warning(
            "Rate limit exceeded for app password provisioning: %s", path_user_id
        )
        return JSONResponse(
            {
                "success": False,
                "error": f"Rate limit exceeded. Try again in {retry_after} seconds.",
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    # Extract and validate BasicAuth credentials
    username, app_password, error_response = _extract_basic_auth(request, path_user_id)
    if error_response is not None:
        _record_rate_limit_attempt(path_user_id, success=False)
        return error_response

    # Validate app password format
    if not APP_PASSWORD_PATTERN.match(app_password):
        _record_rate_limit_attempt(path_user_id, success=False)
        return JSONResponse(
            {"success": False, "error": "Invalid app password format"},
            status_code=400,
        )

    # Parse optional scopes and the Nextcloud loginName from the request body
    # up front. Nextcloud authenticates app passwords against the *loginName*,
    # which can differ from the UID — e.g. OIDC-provisioned users whose UID is
    # their display name (UID "Ada Lovelace", loginName "ada@example.com").
    # Use the loginName for the BasicAuth validation below, falling back to the
    # path user_id for legacy callers that don't send one (where UID ==
    # loginName).
    scopes = None
    nc_username = None
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None  # No / malformed JSON body = legacy call without extras
    if isinstance(body, dict):
        scopes = body.get("scopes")  # list[str] | None
        nc_username = body.get("username")  # Nextcloud loginName

    login_name = nc_username or username

    # Get Nextcloud host from settings
    settings = get_settings()
    nextcloud_host = settings.nextcloud_host

    if not nextcloud_host:
        logger.error("NEXTCLOUD_HOST not configured")
        return JSONResponse(
            {"success": False, "error": "Server not configured"},
            status_code=500,
        )

    # Validate app password against Nextcloud. BasicAuth places the user-id
    # literally in the header (RFC 7617 — no URL-encoding) and Nextcloud keys
    # app-password auth on the loginName, so authenticate as the loginName, not
    # the UID.
    ocs_user_id, error_response = await _validate_nextcloud_credentials(
        nextcloud_host, login_name, app_password
    )
    if error_response is not None:
        _record_rate_limit_attempt(path_user_id, success=False)
        return error_response

    # Verify the authenticated account maps to the path user_id (UID): the
    # loginName must resolve to the UID claimed in the URL path.
    if ocs_user_id != path_user_id:
        logger.warning("User ID mismatch in OCS response")
        _record_rate_limit_attempt(path_user_id, success=False)
        return JSONResponse(
            {"success": False, "error": "User ID mismatch"},
            status_code=403,
        )

    # Store the validated app password
    try:
        storage = await _get_app_password_storage(request)

        await storage.store_app_password_with_scopes(
            username, app_password, scopes=scopes, username=nc_username
        )
        invalidate_scope_cache(username)

        _record_rate_limit_attempt(path_user_id, success=True)
        logger.info("Provisioned app password for user: %s", username)

        return JSONResponse(
            {
                "success": True,
                "message": f"App password stored for {username}",
                "scopes": scopes,
            }
        )

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "provision_app_password")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


async def get_app_password_status(request: Request) -> JSONResponse:
    """GET /api/v1/users/{user_id}/app-password - Check if user has provisioned app password.

    Returns status of background sync access for multi-user BasicAuth mode.

    Requires BasicAuth with the user's app password for authentication.
    """
    # Get user_id from path
    path_user_id = request.path_params.get("user_id")
    if not path_user_id:
        return JSONResponse(
            {"success": False, "error": "Missing user_id in path"},
            status_code=400,
        )

    # Extract and validate BasicAuth credentials
    username, _, error_response = _extract_basic_auth(request, path_user_id)
    if error_response is not None:
        return error_response

    try:
        storage = await _get_app_password_storage(request)
        app_password = await storage.get_app_password(username)

        return JSONResponse(
            {
                "success": True,
                "user_id": username,
                "has_app_password": app_password is not None,
            }
        )

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "get_app_password_status")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


async def delete_app_password(request: Request) -> JSONResponse:
    """DELETE /api/v1/users/{user_id}/app-password - Delete stored app password.

    Removes the user's app password from MCP server storage.

    Requires BasicAuth with the user's credentials.
    """
    # Get user_id from path
    path_user_id = request.path_params.get("user_id")
    if not path_user_id:
        return JSONResponse(
            {"success": False, "error": "Missing user_id in path"},
            status_code=400,
        )

    # Extract and validate BasicAuth credentials
    username, password, error_response = _extract_basic_auth(request, path_user_id)
    if error_response is not None:
        return error_response

    # Nextcloud keys app-password auth on the loginName, which can differ from
    # the UID (OIDC-provisioned users). Use the loginName from the body when
    # present, falling back to the path UID for legacy callers — same contract
    # as provisioning.
    nc_username = None
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None  # No / malformed JSON body = legacy call without a loginName
    if isinstance(body, dict):
        nc_username = body.get("username")
    login_name = nc_username or username

    # Validate credentials against Nextcloud. Shares the OCS v2 + defensive
    # parsing path with provisioning (issue #824): OCS v1 always returned HTTP
    # 200, so the old ``!= 200`` guard never fired and a bad credential could
    # silently pass — a genuine auth bypass on deletion.
    settings = get_settings()
    nextcloud_host = settings.nextcloud_host

    if not nextcloud_host:
        logger.error("NEXTCLOUD_HOST not configured")
        return JSONResponse(
            {"success": False, "error": "Server not configured"},
            status_code=500,
        )

    # Keep this route's historical "Invalid credentials" wording via the helper
    # rather than unwrapping and rebuilding its response.
    ocs_user_id, error_response = await _validate_nextcloud_credentials(
        nextcloud_host,
        login_name,
        password,
        invalid_credential_error="Invalid credentials",
    )
    if error_response is not None:
        return error_response

    # The authenticated account must be the UID whose password is being deleted.
    # ``_extract_basic_auth`` only checks the BasicAuth *name* field equals the
    # path UID, not that the supplied credential authenticates as that account —
    # without this guard a user could authenticate with their own loginName (via
    # the body) while targeting another user's path and delete the victim's
    # stored password.
    if ocs_user_id != path_user_id:
        logger.warning("User ID mismatch in OCS response for delete")
        return JSONResponse(
            {"success": False, "error": "User ID mismatch"},
            status_code=403,
        )

    try:
        storage = await _get_app_password_storage(request)
        deleted = await storage.delete_app_password(username)

        if deleted:
            logger.info("Deleted app password for user: %s", username)
            return JSONResponse(
                {
                    "success": True,
                    "message": f"App password deleted for {username}",
                }
            )
        else:
            return JSONResponse(
                {
                    "success": True,
                    "message": "No app password found to delete",
                }
            )

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "delete_app_password")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )
