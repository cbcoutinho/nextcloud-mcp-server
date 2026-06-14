"""Webhook management API endpoints.

Provides REST API endpoints for managing webhook registrations with Nextcloud.
These endpoints are used by the Nextcloud PHP app (Astrolabe) to:
- List installed Nextcloud apps
- Create, list, and delete webhook registrations

All endpoints require OAuth bearer token authentication via UnifiedTokenVerifier.

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
from nextcloud_mcp_server.auth.webhook_routes import (
    WebhookSecretNotConfigured,
    webhook_auth_pair,
)
from nextcloud_mcp_server.client.webhooks import WebhooksClient

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


async def list_webhooks(request: Request) -> JSONResponse:
    """GET /api/v1/webhooks - List all registered webhooks.

    Returns list of webhook registrations for the authenticated user.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/webhooks: %s", e)
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "list_webhooks"),
            },
            status_code=401,
        )

    try:
        username, app_password = await get_basic_auth_for_user(user_id)

        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")
        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        async with nextcloud_httpx_client(
            base_url=nextcloud_host,
            auth=httpx.BasicAuth(username, app_password),
            timeout=30.0,
        ) as client:
            webhooks_client = WebhooksClient(client, username)
            webhooks = await webhooks_client.list_webhooks()
            return JSONResponse({"webhooks": webhooks})

    except ProvisioningRequiredError as e:
        logger.info("Provisioning required for user %s: %s", user_id, e)
        return JSONResponse(
            {"error": "Provisioning required", "message": str(e)},
            status_code=428,
        )
    except Exception as e:
        logger.error("Error listing webhooks for user %s: %s", user_id, e)
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "list_webhooks"),
            },
            status_code=500,
        )


async def create_webhook(request: Request) -> JSONResponse:
    """POST /api/v1/webhooks - Create a new webhook registration.

    Request body:
    {
        "event": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
        "uri": "http://mcp:8000/webhooks/nextcloud",
        "eventFilter": {"event.node.path": "/^\\/.*\\/files\\/Notes\\//"}
    }

    Returns the created webhook data including the webhook ID.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/webhooks: %s", e)
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "create_webhook"),
            },
            status_code=401,
        )

    try:
        # Parse request body
        body = await request.json()
        event = body.get("event")
        uri = body.get("uri")
        # Accept both camelCase (eventFilter) and snake_case (event_filter)
        event_filter = body.get("eventFilter") or body.get("event_filter")

        if not event or not uri:
            return JSONResponse(
                {
                    "error": "Bad request",
                    "message": "Missing required fields: event, uri",
                },
                status_code=400,
            )

        username, app_password = await get_basic_auth_for_user(user_id)

        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")
        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        async with nextcloud_httpx_client(
            base_url=nextcloud_host,
            auth=httpx.BasicAuth(username, app_password),
            timeout=30.0,
        ) as client:
            # Inject delivery auth headers when WEBHOOK_SECRET is configured so
            # that webhook deliveries from Nextcloud back to us are authenticated.
            webhooks_client = WebhooksClient(client, username)
            try:
                auth_method, auth_data = webhook_auth_pair()
            except WebhookSecretNotConfigured as e:
                logger.warning(
                    "Webhook registration refused for user %s: %s", user_id, e
                )
                return JSONResponse(
                    {
                        "error": "Webhooks disabled",
                        "message": str(e),
                    },
                    status_code=503,
                )
            webhook_data = await webhooks_client.create_webhook(
                event=event,
                uri=uri,
                event_filter=event_filter,
                auth_method=auth_method,
                auth_data=auth_data,
            )

            return JSONResponse({"webhook": webhook_data})

    except ProvisioningRequiredError as e:
        logger.info("Provisioning required for user %s: %s", user_id, e)
        return JSONResponse(
            {"error": "Provisioning required", "message": str(e)},
            status_code=428,
        )
    except Exception as e:
        logger.error("Error creating webhook for user %s: %s", user_id, e)
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "create_webhook"),
            },
            status_code=500,
        )


async def delete_webhook(request: Request) -> JSONResponse:
    """DELETE /api/v1/webhooks/{webhook_id} - Delete a webhook registration.

    Returns success/failure status.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/webhooks: %s", e)
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "delete_webhook"),
            },
            status_code=401,
        )

    try:
        # Get webhook_id from path parameter
        webhook_id = request.path_params.get("webhook_id")
        if not webhook_id:
            return JSONResponse(
                {"error": "Bad request", "message": "Missing webhook_id"},
                status_code=400,
            )

        try:
            webhook_id = int(webhook_id)
        except ValueError:
            return JSONResponse(
                {"error": "Bad request", "message": "Invalid webhook_id"},
                status_code=400,
            )

        username, app_password = await get_basic_auth_for_user(user_id)

        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")
        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        async with nextcloud_httpx_client(
            base_url=nextcloud_host,
            auth=httpx.BasicAuth(username, app_password),
            timeout=30.0,
        ) as client:
            webhooks_client = WebhooksClient(client, username)
            await webhooks_client.delete_webhook(webhook_id=webhook_id)
            return JSONResponse({"success": True, "message": "Webhook deleted"})

    except ProvisioningRequiredError as e:
        logger.info("Provisioning required for user %s: %s", user_id, e)
        return JSONResponse(
            {"error": "Provisioning required", "message": str(e)},
            status_code=428,
        )
    except Exception as e:
        logger.error("Error deleting webhook for user %s: %s", user_id, e)
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "delete_webhook"),
            },
            status_code=500,
        )
