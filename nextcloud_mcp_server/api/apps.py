"""Installed-apps API endpoint.

Serves ``GET /api/v1/apps`` — the ids of the installed Nextcloud apps, used by
the Nextcloud PHP app (Astrolabe) to decide which features to surface.

Requires OAuth bearer token authentication via UnifiedTokenVerifier.

Auth model: the OAuth bearer is validated at the perimeter to identify the
user (``validate_token_and_get_user``); calls to Nextcloud are then made with
the user's stored app password via HTTP Basic Auth (see
``docs/login-flow-v2.md`` and ADR-022). The OAuth bearer is NEVER forwarded
to Nextcloud — that pattern depended on upstream user_oidc patches that were
never merged and is incompatible with admin endpoints gated by
``@PasswordConfirmationRequired``.
"""

import logging

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.api._auth import get_basic_auth_for_user
from nextcloud_mcp_server.api.management import (
    _sanitize_error_for_client,
    validate_token_and_get_user,
)
from nextcloud_mcp_server.auth.scope_authorization import ProvisioningRequiredError

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


async def get_installed_apps(request: Request) -> JSONResponse:
    """GET /api/v1/apps - Get list of installed Nextcloud apps.

    Returns a list of installed app IDs for filtering webhook presets.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/apps: %s", e)
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "get_installed_apps"),
            },
            status_code=401,
        )

    try:
        username, app_password = await get_basic_auth_for_user(user_id)

        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")
        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # OCS v2 capabilities is keyed by app-id for every enabled app that
        # implements OCSCapabilities — sufficient for the webhook presets UI
        # without needing the admin-only /cloud/apps endpoint.
        async with nextcloud_httpx_client(
            base_url=nextcloud_host,
            auth=httpx.BasicAuth(username, app_password),
            timeout=30.0,
        ) as client:
            response = await client.get(
                "/ocs/v2.php/cloud/capabilities",
                params={"format": "json"},
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            )

            if response.status_code != 200:
                raise ValueError(f"OCS API returned status {response.status_code}")

            data = response.json()
            capabilities = data.get("ocs", {}).get("data", {}).get("capabilities", {})
            apps = sorted(capabilities.keys())

            return JSONResponse({"apps": apps})

    except ProvisioningRequiredError as e:
        logger.info("Provisioning required for user %s: %s", user_id, e)
        return JSONResponse(
            {"error": "Provisioning required", "message": str(e)},
            status_code=428,
        )
    except Exception as e:
        logger.error("Error getting installed apps for user %s: %s", user_id, e)
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "get_installed_apps"),
            },
            status_code=500,
        )
