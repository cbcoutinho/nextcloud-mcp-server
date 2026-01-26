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
from typing import TYPE_CHECKING

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

from nextcloud_mcp_server.api.management import _sanitize_error_for_client

logger = logging.getLogger(__name__)

# App password format regex (Nextcloud format: xxxxx-xxxxx-xxxxx-xxxxx-xxxxx)
APP_PASSWORD_PATTERN = re.compile(
    r"^[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}$"
)

# Timeout for Nextcloud API validation requests (seconds)
NEXTCLOUD_VALIDATION_TIMEOUT = 10.0

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
            f"Username mismatch in app password operation for path user {path_user_id}"
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


async def _get_app_password_storage(request: Request) -> "RefreshTokenStorage":
    """Get or initialize RefreshTokenStorage for app password operations.

    Checks app.state.storage first, then falls back to creating from environment.
    This helper avoids repeated storage initialization logic across endpoints.

    Args:
        request: Starlette request with app state

    Returns:
        Initialized RefreshTokenStorage instance
    """
    from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

    storage = getattr(request.app.state, "storage", None)

    if not storage:
        # Multi-user BasicAuth mode may not have oauth_context
        # Initialize storage from environment
        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

    return storage


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
    from nextcloud_mcp_server.config import get_settings

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
            f"Rate limit exceeded for app password provisioning: {path_user_id}"
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

    # Get Nextcloud host from settings
    settings = get_settings()
    nextcloud_host = settings.nextcloud_host

    if not nextcloud_host:
        logger.error("NEXTCLOUD_HOST not configured")
        return JSONResponse(
            {"success": False, "error": "Server not configured"},
            status_code=500,
        )

    # Validate app password against Nextcloud
    try:
        async with httpx.AsyncClient(timeout=NEXTCLOUD_VALIDATION_TIMEOUT) as client:
            # Use OCS API to verify credentials
            test_url = f"{nextcloud_host}/ocs/v1.php/cloud/user"
            response = await client.get(
                test_url,
                auth=(username, app_password),
                params={"format": "json"},
                headers={"OCS-APIRequest": "true"},
            )

            if response.status_code != 200:
                logger.warning(
                    f"App password validation failed for user: HTTP {response.status_code}"
                )
                _record_rate_limit_attempt(path_user_id, success=False)
                return JSONResponse(
                    {"success": False, "error": "Invalid app password"},
                    status_code=401,
                )

            # Verify the user ID from response matches
            data = response.json()
            ocs_user_id = data.get("ocs", {}).get("data", {}).get("id")
            if ocs_user_id != username:
                logger.warning("User ID mismatch in OCS response")
                _record_rate_limit_attempt(path_user_id, success=False)
                return JSONResponse(
                    {"success": False, "error": "User ID mismatch"},
                    status_code=403,
                )

    except httpx.RequestError as e:
        logger.error(f"Failed to validate app password: {e}")
        return JSONResponse(
            {"success": False, "error": "Failed to validate credentials"},
            status_code=500,
        )

    # Store the validated app password
    try:
        storage = await _get_app_password_storage(request)
        await storage.store_app_password(username, app_password)

        _record_rate_limit_attempt(path_user_id, success=True)
        logger.info(f"Provisioned app password for user: {username}")

        return JSONResponse(
            {
                "success": True,
                "message": f"App password stored for {username}",
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
    from nextcloud_mcp_server.config import get_settings

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

    # Validate credentials against Nextcloud
    settings = get_settings()
    nextcloud_host = settings.nextcloud_host

    try:
        async with httpx.AsyncClient(timeout=NEXTCLOUD_VALIDATION_TIMEOUT) as client:
            test_url = f"{nextcloud_host}/ocs/v1.php/cloud/user"
            response = await client.get(
                test_url,
                auth=(username, password),
                params={"format": "json"},
                headers={"OCS-APIRequest": "true"},
            )

            if response.status_code != 200:
                return JSONResponse(
                    {"success": False, "error": "Invalid credentials"},
                    status_code=401,
                )
    except httpx.RequestError as e:
        logger.error(f"Failed to validate credentials: {e}")
        return JSONResponse(
            {"success": False, "error": "Failed to validate credentials"},
            status_code=500,
        )

    try:
        storage = await _get_app_password_storage(request)
        deleted = await storage.delete_app_password(username)

        if deleted:
            logger.info(f"Deleted app password for user: {username}")
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
