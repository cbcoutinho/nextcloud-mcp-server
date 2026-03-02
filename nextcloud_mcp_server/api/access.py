"""Access and scope management API endpoints.

Provides REST API endpoints for querying and managing user access status
and application-level scopes for Login Flow v2 mode.
"""

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.api.management import _sanitize_error_for_client
from nextcloud_mcp_server.api.passwords import (
    _extract_basic_auth,
    _get_app_password_storage,
)
from nextcloud_mcp_server.models.auth import ALL_SUPPORTED_SCOPES

logger = logging.getLogger(__name__)


async def get_user_access(request: Request) -> JSONResponse:
    """GET /api/v1/users/{user_id}/access - Get user's provisioned access and scopes.

    Returns the user's current provisioning status, granted scopes, and metadata.
    Requires BasicAuth with the user's credentials.
    """
    path_user_id = request.path_params.get("user_id")
    if not path_user_id:
        return JSONResponse(
            {"success": False, "error": "Missing user_id in path"},
            status_code=400,
        )

    username, _, error_response = _extract_basic_auth(request, path_user_id)
    if error_response is not None:
        return error_response

    try:
        storage = await _get_app_password_storage(request)
        data = await storage.get_app_password_with_scopes(username)

        if data is None:
            return JSONResponse(
                {
                    "success": True,
                    "user_id": username,
                    "provisioned": False,
                    "scopes": None,
                    "username": None,
                }
            )

        return JSONResponse(
            {
                "success": True,
                "user_id": username,
                "provisioned": True,
                "scopes": data["scopes"],
                "username": data.get("username"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
            }
        )

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "get_user_access")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


async def update_user_scopes(request: Request) -> JSONResponse:
    """PATCH /api/v1/users/{user_id}/scopes - Update user's application-level scopes.

    Accepts JSON body with:
    - scopes: list[str] - New scope set to apply

    This only updates the stored scopes, not the app password itself.
    The app password remains valid; scope enforcement is application-level.
    """
    path_user_id = request.path_params.get("user_id")
    if not path_user_id:
        return JSONResponse(
            {"success": False, "error": "Missing user_id in path"},
            status_code=400,
        )

    username, _, error_response = _extract_basic_auth(request, path_user_id)
    if error_response is not None:
        return error_response

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"success": False, "error": "Invalid JSON body"},
            status_code=400,
        )

    scopes = body.get("scopes")
    if scopes is None or not isinstance(scopes, list):
        return JSONResponse(
            {"success": False, "error": "scopes must be a list of strings"},
            status_code=400,
        )

    # Validate scopes
    invalid = [s for s in scopes if s not in ALL_SUPPORTED_SCOPES]
    if invalid:
        return JSONResponse(
            {
                "success": False,
                "error": f"Invalid scopes: {', '.join(invalid)}",
                "valid_scopes": ALL_SUPPORTED_SCOPES,
            },
            status_code=400,
        )

    try:
        storage = await _get_app_password_storage(request)
        existing = await storage.get_app_password_with_scopes(username)

        if existing is None:
            return JSONResponse(
                {
                    "success": False,
                    "error": "No app password provisioned for this user",
                },
                status_code=404,
            )

        # Update scopes only (no decrypt/re-encrypt of the password)
        await storage.update_app_password_scopes(
            user_id=username,
            scopes=scopes,
        )

        return JSONResponse(
            {
                "success": True,
                "user_id": username,
                "scopes": scopes,
                "message": "Scopes updated successfully",
            }
        )

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "update_user_scopes")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )


async def list_supported_scopes(_: Request) -> JSONResponse:
    """GET /api/v1/scopes - List all supported application-level scopes."""
    return JSONResponse(
        {
            "success": True,
            "scopes": ALL_SUPPORTED_SCOPES,
        }
    )
