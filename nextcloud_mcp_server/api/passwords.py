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
from collections.abc import Callable

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

    # Count all recorded attempts in the window. provision_app_password records
    # both successes and failures; the read/scope/status/delete routes (via
    # _authenticate_request) record only failures so legitimate polling with a
    # correct credential is never throttled.
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


def _check_app_password_format(app_password: str) -> JSONResponse | None:
    """Shape guard for a provisioned app password (see ``APP_PASSWORD_PATTERN``).

    Returns a ready-to-return 400 :class:`JSONResponse` when the value isn't a
    plausible app-password token, else ``None``. The authoritative check is the
    OCS validation; this only rejects obvious garbage before the round-trip.
    """
    if not APP_PASSWORD_PATTERN.match(app_password):
        return JSONResponse(
            {"success": False, "error": "Invalid app password format"},
            status_code=400,
        )
    return None


async def _authenticate_request(
    request: Request,
    path_user_id: str,
    *,
    invalid_credential_error: str = "Invalid app password",
    validate_password: Callable[[str], JSONResponse | None] | None = None,
) -> tuple[str, str, JSONResponse | None]:
    """Authenticate a user-management request against Nextcloud.

    Combines every check these endpoints need before touching stored state:

    1. A BasicAuth header is present and its *name* field equals the path
       ``user_id`` (:func:`_extract_basic_auth`).
    2. The supplied **password actually authenticates** against Nextcloud — the
       check GHSA-x88r-fhx7-52h6 found missing on the scope/access/status
       routes, where a username/path string compare alone was mistaken for
       authentication, letting anyone who knew a victim's username read or
       rewrite that victim's stored scopes.
    3. The authenticated Nextcloud account's UID equals the path ``user_id``,
       so a caller can only act on their own record (defends against the same
       cross-user pivot guarded in ``delete_app_password``).

    Nextcloud keys app-password auth on the *loginName*, which can differ from
    the UID (e.g. OIDC-provisioned users — UID "Ada Lovelace", loginName
    "ada@example.com"). Callers may pass the loginName in a ``username`` body
    field; we fall back to the path UID for legacy callers where the two match.
    The body is parsed via Starlette's cached ``request.json()``, so a caller
    that reads the body again afterwards is unaffected.

    Unless a ``validate_password`` hook rejects the credential early, each call
    costs a Nextcloud OCS round-trip, so the per-user sliding-window rate limiter
    shared with provisioning throttles brute-force. Only *failed* attempts are
    recorded, so a legitimate client polling these endpoints with a correct
    credential is never throttled — but repeated wrong passwords for a given user
    hit the cap (``RATE_LIMIT_MAX_ATTEMPTS``/hour).

    ``validate_password`` is an optional hook run on the extracted password
    *after* the BasicAuth name check but *before* the OCS round-trip — used by
    provisioning to reject malformed app passwords (saving a round-trip) without
    duplicating the rest of the auth flow. It returns an error
    :class:`JSONResponse` to reject, or ``None`` to proceed.

    Returns ``(uid, password, None)`` on success — where ``uid`` is the
    Nextcloud-canonical UID — otherwise ``("", "", error_response)`` with a
    ready-to-return :class:`JSONResponse`.
    """
    # Extract + name-match FIRST, before touching rate-limit state. A missing,
    # malformed, or wrong-username Authorization header is rejected immediately
    # without consuming the victim's rate-limit budget — otherwise an
    # unauthenticated request flood (no credential needed) could lock a victim
    # out of their own endpoints (request-flood DoS vs. the brute-force the
    # limiter is meant to stop).
    username, password, error_response = _extract_basic_auth(request, path_user_id)
    if error_response is not None:
        return "", "", error_response

    # Now throttle: only requests that present the victim's username (a genuine
    # credential-guess attempt) count toward the per-user brute-force limiter.
    is_allowed, retry_after = _check_rate_limit(path_user_id)
    if not is_allowed:
        logger.warning(
            "Rate limit exceeded for user-management request: %s", path_user_id
        )
        return (
            "",
            "",
            JSONResponse(
                {
                    "success": False,
                    "error": f"Rate limit exceeded. Try again in {retry_after} seconds.",
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            ),
        )

    if validate_password is not None:
        format_error = validate_password(password)
        if format_error is not None:
            _record_rate_limit_attempt(path_user_id, success=False)
            return "", "", format_error

    # Optional Nextcloud loginName from the body (OIDC users where UID !=
    # loginName). A missing/malformed body just means "legacy caller" — fall
    # back to the path UID.
    nc_username = None
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict):
        nc_username = body.get("username")
    login_name = nc_username or username

    settings = get_settings()
    nextcloud_host = settings.nextcloud_host
    if not nextcloud_host:
        logger.error("NEXTCLOUD_HOST not configured")
        return (
            "",
            "",
            JSONResponse(
                {"success": False, "error": "Server not configured"},
                status_code=500,
            ),
        )

    ocs_user_id, error_response = await _validate_nextcloud_credentials(
        nextcloud_host,
        login_name,
        password,
        invalid_credential_error=invalid_credential_error,
    )
    if error_response is not None:
        _record_rate_limit_attempt(path_user_id, success=False)
        return "", "", error_response

    # The authenticated account must own the path UID. ``_extract_basic_auth``
    # only checks the BasicAuth *name* field, not that the credential
    # authenticates as that account — without this a user could authenticate as
    # their own loginName (via the body) while targeting another user's path.
    if ocs_user_id != path_user_id:
        logger.warning("User ID mismatch in OCS response")
        _record_rate_limit_attempt(path_user_id, success=False)
        return (
            "",
            "",
            JSONResponse(
                {"success": False, "error": "User ID mismatch"},
                status_code=403,
            ),
        )

    # Return the canonical UID. By the checks above it equals
    # ``ocs_user_id == username == path_user_id``; use ``path_user_id`` as it is
    # statically ``str`` (``ocs_user_id`` is ``str | None`` from the validator).
    return path_user_id, password, None


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

    # Full credential gate, shared with the read/scope/delete endpoints: rate
    # limit → BasicAuth name match → app-password format → OCS validation → UID
    # ownership. The format guard runs (via ``validate_password``) before the OCS
    # round-trip so malformed tokens still 400 without hitting Nextcloud.
    username, app_password, error_response = await _authenticate_request(
        request, path_user_id, validate_password=_check_app_password_format
    )
    if error_response is not None:
        return error_response

    # Re-read the (cached) body for the extras provisioning needs: the optional
    # scope set, and the Nextcloud loginName for storage — Nextcloud keys
    # app-password auth on the loginName, which can differ from the UID (e.g.
    # OIDC users: UID "Ada Lovelace", loginName "ada@example.com").
    scopes = None
    nc_username = None
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None  # No / malformed JSON body = legacy call without extras
    if isinstance(body, dict):
        scopes = body.get("scopes")  # list[str] | None
        nc_username = body.get("username")  # Nextcloud loginName

    # Store the validated app password
    try:
        storage = await _get_app_password_storage(request)

        await storage.store_app_password_with_scopes(
            username, app_password, scopes=scopes, username=nc_username
        )
        invalidate_scope_cache(username)
        # Wake the background sync user manager so this user's scanner starts
        # now instead of after the next poll. Local import avoids an app <->
        # api-module import cycle.
        from nextcloud_mcp_server.app import notify_user_provisioned  # noqa: PLC0415

        notify_user_provisioned()

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

    Requires BasicAuth with the user's app password, validated against Nextcloud
    (username-equality alone is not authentication — GHSA-x88r-fhx7-52h6).
    """
    # Get user_id from path
    path_user_id = request.path_params.get("user_id")
    if not path_user_id:
        return JSONResponse(
            {"success": False, "error": "Missing user_id in path"},
            status_code=400,
        )

    # Authenticate against Nextcloud: BasicAuth name match + password validation
    # + UID ownership.
    username, _, error_response = await _authenticate_request(request, path_user_id)
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

    # Authenticate against Nextcloud: BasicAuth name match + password validation
    # + UID ownership (shared with the other management endpoints). Validating
    # the credential here closes the OCS v1 ``!= 200`` bypass (issue #824) and
    # the cross-user pivot — a wrong password, or a credential that authenticates
    # as a different account than the path UID, can no longer delete a victim's
    # stored password. The "Invalid credentials" wording is this route's
    # historical 401 text.
    username, _, error_response = await _authenticate_request(
        request, path_user_id, invalid_credential_error="Invalid credentials"
    )
    if error_response is not None:
        return error_response

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
