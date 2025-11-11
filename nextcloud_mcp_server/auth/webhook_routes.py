"""Webhook management routes for admin UI.

Provides browser-based endpoints for admin users to manage webhook configurations
using preset templates. Only accessible to Nextcloud administrators.
"""

import logging
import os

import httpx
from starlette.authentication import requires
from starlette.requests import Request
from starlette.responses import HTMLResponse

from nextcloud_mcp_server.auth.permissions import is_nextcloud_admin
from nextcloud_mcp_server.client.webhooks import WebhooksClient
from nextcloud_mcp_server.server.webhook_presets import (
    WEBHOOK_PRESETS,
    filter_presets_by_installed_apps,
    get_preset,
)

logger = logging.getLogger(__name__)


def _get_storage(request: Request):
    """Get storage instance from app state.

    Args:
        request: Starlette request object

    Returns:
        RefreshTokenStorage instance or None
    """
    # Try browser_app state first (for /user routes)
    storage = getattr(request.app.state, "storage", None)

    # Try oauth_context if in OAuth mode
    if not storage:
        oauth_ctx = getattr(request.app.state, "oauth_context", None)
        if oauth_ctx:
            storage = oauth_ctx.get("storage")

    return storage


async def _get_installed_apps(http_client: httpx.AsyncClient) -> list[str]:
    """Get list of installed and enabled apps from Nextcloud capabilities.

    Args:
        http_client: Authenticated HTTP client

    Returns:
        List of installed app names (e.g., ["notes", "calendar", "forms"])
    """
    try:
        response = await http_client.get(
            "/ocs/v2.php/cloud/capabilities",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

        # Extract app names from capabilities
        capabilities = data.get("ocs", {}).get("data", {}).get("capabilities", {})
        # Filter out core NC capabilities (not apps)
        core_keys = {"version", "core"}
        app_keys = set(capabilities.keys()) - core_keys
        return sorted(app_keys)
    except Exception as e:
        logger.warning(f"Failed to get installed apps from capabilities: {e}")
        return []


def _get_webhook_uri() -> str:
    """Get the webhook endpoint URI for this MCP server.

    This function determines the correct webhook URL based on the environment:
    1. Uses WEBHOOK_INTERNAL_URL if explicitly set (highest priority)
    2. Detects Docker environment and uses internal service name
    3. Falls back to NEXTCLOUD_MCP_SERVER_URL

    In Docker environments, Nextcloud needs to reach the MCP service using
    the internal Docker network hostname (e.g., http://mcp:8000), not localhost.

    Returns:
        Full webhook endpoint URL accessible from Nextcloud
    """
    # Explicit override (highest priority)
    webhook_url = os.getenv("WEBHOOK_INTERNAL_URL")
    if webhook_url:
        return f"{webhook_url}/webhooks/nextcloud"

    # Detect Docker environment
    # Check for common Docker indicators
    is_docker = (
        os.path.exists("/.dockerenv")  # Docker container marker file
        or os.path.exists("/run/.containerenv")  # Podman marker
        or os.getenv("DOCKER_CONTAINER") == "true"  # Explicit flag
    )

    if is_docker:
        # In Docker, use internal service name from NEXTCLOUD_MCP_SERVICE_NAME
        # or default to 'mcp' (docker-compose service name)
        service_name = os.getenv("NEXTCLOUD_MCP_SERVICE_NAME", "mcp")
        port = os.getenv("NEXTCLOUD_MCP_PORT", "8000")
        logger.debug(
            f"Docker environment detected, using internal URL: http://{service_name}:{port}"
        )
        return f"http://{service_name}:{port}/webhooks/nextcloud"

    # Fallback to configured server URL (for non-Docker deployments)
    server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")
    return f"{server_url}/webhooks/nextcloud"


async def _get_authenticated_client(request: Request) -> httpx.AsyncClient:
    """Get an authenticated HTTP client for Nextcloud API calls.

    Args:
        request: Starlette request object

    Returns:
        Authenticated httpx.AsyncClient

    Raises:
        RuntimeError: If unable to create authenticated client
    """
    # Get OAuth context from app state
    oauth_ctx = getattr(request.app.state, "oauth_context", None)

    # BasicAuth mode - use credentials from environment
    if not oauth_ctx:
        nextcloud_host = os.getenv("NEXTCLOUD_HOST")
        username = os.getenv("NEXTCLOUD_USERNAME")
        password = os.getenv("NEXTCLOUD_PASSWORD")

        if not all([nextcloud_host, username, password]):
            raise RuntimeError("BasicAuth credentials not configured")

        assert nextcloud_host is not None  # Type narrowing for type checker
        return httpx.AsyncClient(
            base_url=nextcloud_host,
            auth=(username, password),
            timeout=30.0,
        )

    # OAuth mode - get token from session
    storage = oauth_ctx.get("storage")
    session_id = request.cookies.get("mcp_session")

    if not storage or not session_id:
        raise RuntimeError("Session not found")

    token_data = await storage.get_refresh_token(session_id)
    if not token_data or "access_token" not in token_data:
        raise RuntimeError("No access token found in session")

    access_token = token_data["access_token"]
    nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

    if not nextcloud_host:
        raise RuntimeError("Nextcloud host not configured")

    return httpx.AsyncClient(
        base_url=nextcloud_host,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30.0,
    )


async def _get_enabled_presets(
    webhooks_client: WebhooksClient,
    storage=None,
) -> dict[str, list[int]]:
    """Get currently enabled webhook presets.

    Reads from database first for better performance. Falls back to API if needed.

    Args:
        webhooks_client: Webhooks API client
        storage: Optional RefreshTokenStorage instance

    Returns:
        Dictionary mapping preset_id to list of webhook IDs
    """
    try:
        # Try database first (faster, works offline)
        if storage:
            all_webhooks = await storage.list_all_webhooks()
            enabled_presets: dict[str, list[int]] = {}

            for webhook in all_webhooks:
                preset_id = webhook["preset_id"]
                webhook_id = webhook["webhook_id"]

                if preset_id not in enabled_presets:
                    enabled_presets[preset_id] = []
                enabled_presets[preset_id].append(webhook_id)

            return enabled_presets

        # Fallback to API query
        registered_webhooks = await webhooks_client.list_webhooks()
        webhook_uri = _get_webhook_uri()

        # Group webhooks by preset based on matching events
        enabled_presets: dict[str, list[int]] = {}

        for preset_id, preset in WEBHOOK_PRESETS.items():
            preset_event_classes = {event["event"] for event in preset["events"]}
            matching_webhooks = []

            for webhook in registered_webhooks:
                # Check if webhook matches this preset
                if (
                    webhook.get("uri") == webhook_uri
                    and webhook.get("event") in preset_event_classes
                ):
                    matching_webhooks.append(webhook["id"])

            if matching_webhooks:
                enabled_presets[preset_id] = matching_webhooks

        return enabled_presets

    except Exception as e:
        logger.error(f"Failed to list webhooks: {e}")
        return {}


@requires("authenticated", redirect="oauth_login")
async def webhook_management_pane(request: Request) -> HTMLResponse:
    """Webhook management pane - returns HTML for webhook configuration.

    This endpoint checks if the user is an admin and returns either:
    - Admin view: Webhook management interface with preset controls
    - Non-admin view: Message indicating admin-only access

    Args:
        request: Starlette request object

    Returns:
        HTML response with webhook management interface or access denied message
    """
    try:
        # Get authenticated HTTP client
        http_client = await _get_authenticated_client(request)
        username = request.user.display_name

        # Check admin permissions
        is_admin = await is_nextcloud_admin(request, http_client)

        if not is_admin:
            return HTMLResponse(
                content="""
                <div class="info-message">
                    <p><strong>Admin Access Required</strong></p>
                    <p>Webhook management is only available to Nextcloud administrators.</p>
                    <p>Your account does not have admin privileges.</p>
                </div>
                """
            )

        # Get webhooks client
        webhooks_client = WebhooksClient(http_client, username)

        # Get storage for database-backed webhook tracking
        storage = _get_storage(request)

        # Get installed apps to filter presets
        installed_apps = await _get_installed_apps(http_client)
        logger.debug(f"Installed apps: {installed_apps}")

        # Get currently enabled presets (from database or API)
        enabled_presets = await _get_enabled_presets(webhooks_client, storage)

        # Filter presets based on installed apps
        available_presets = filter_presets_by_installed_apps(installed_apps)

        # Build preset cards HTML
        preset_cards_html = ""
        for preset_id, preset in available_presets:
            is_enabled = preset_id in enabled_presets
            num_webhooks = len(enabled_presets.get(preset_id, []))

            # Status badge
            if is_enabled:
                status_badge = f'<span style="color: #4caf50; font-weight: bold;">✓ Enabled ({num_webhooks} webhooks)</span>'
                action_button = f"""
                <button
                    hx-delete="/user/webhooks/disable/{preset_id}"
                    hx-target="#preset-{preset_id}"
                    hx-swap="outerHTML"
                    class="button"
                    style="background-color: #ff9800;">
                    Disable
                </button>
                """
            else:
                status_badge = '<span style="color: #999;">Not Enabled</span>'
                action_button = f"""
                <button
                    hx-post="/user/webhooks/enable/{preset_id}"
                    hx-target="#preset-{preset_id}"
                    hx-swap="outerHTML"
                    class="button button-primary">
                    Enable
                </button>
                """

            preset_cards_html += f"""
            <div id="preset-{preset_id}" style="border: 1px solid #e0e0e0; border-radius: 6px; padding: 20px; margin: 15px 0;">
                <h3 style="margin-top: 0; color: #0082c9;">{preset["name"]}</h3>
                <p style="color: #666; margin: 10px 0;">{preset["description"]}</p>
                <p style="font-size: 13px; color: #999;">
                    <strong>App:</strong> {preset["app"]} |
                    <strong>Events:</strong> {len(preset["events"])}
                </p>
                <div style="margin-top: 15px; display: flex; align-items: center; gap: 15px;">
                    <div>{status_badge}</div>
                    <div>{action_button}</div>
                </div>
            </div>
            """

        # Get webhook endpoint URL for display
        webhook_uri = _get_webhook_uri()

        html_content = f"""
        <h2>Webhook Management</h2>
        <div class="info-message">
            <p><strong>About Webhooks</strong></p>
            <p>Webhooks enable real-time synchronization by notifying this server when content changes in Nextcloud.</p>
            <p><strong>Endpoint:</strong> <code>{webhook_uri}</code></p>
        </div>

        <h3 style="margin-top: 30px;">Available Presets</h3>
        <p style="color: #666;">Enable webhook presets with one click for common synchronization scenarios.</p>
        <p style="color: #999; font-size: 13px; margin-top: 5px;">Showing {len(available_presets)} preset(s) for your installed apps ({len(installed_apps)} detected)</p>

        {preset_cards_html}
        """

        return HTMLResponse(content=html_content)

    except Exception as e:
        logger.error(f"Error loading webhook management pane: {e}", exc_info=True)
        return HTMLResponse(
            content=f"""
            <div class="warning">
                <p><strong>Error Loading Webhooks</strong></p>
                <p>{str(e)}</p>
            </div>
            """,
            status_code=500,
        )


@requires("authenticated", redirect="oauth_login")
async def enable_webhook_preset(request: Request) -> HTMLResponse:
    """Enable a webhook preset by registering all webhooks.

    Args:
        request: Starlette request object (preset_id in path)

    Returns:
        HTML response with updated preset card
    """
    preset_id = request.path_params["preset_id"]

    try:
        # Get authenticated HTTP client
        http_client = await _get_authenticated_client(request)
        username = request.user.display_name

        # Check admin permissions
        is_admin = await is_nextcloud_admin(request, http_client)
        if not is_admin:
            return HTMLResponse(
                content='<div class="warning">Admin access required</div>',
                status_code=403,
            )

        # Get preset configuration
        preset = get_preset(preset_id)
        if not preset:
            return HTMLResponse(
                content=f'<div class="warning">Unknown preset: {preset_id}</div>',
                status_code=404,
            )

        # Register webhooks
        webhooks_client = WebhooksClient(http_client, username)
        webhook_uri = _get_webhook_uri()
        registered_ids = []

        for event_config in preset["events"]:
            webhook_data = await webhooks_client.create_webhook(
                event=event_config["event"],
                uri=webhook_uri,
                event_filter=event_config["filter"] if event_config["filter"] else None,
            )
            webhook_id = webhook_data["id"]
            registered_ids.append(webhook_id)
            logger.info(f"Registered webhook {webhook_id} for {event_config['event']}")

        # Persist webhook IDs to database
        storage = _get_storage(request)
        if storage:
            for webhook_id in registered_ids:
                await storage.store_webhook(webhook_id, preset_id)
            logger.info(
                f"Persisted {len(registered_ids)} webhook(s) for preset '{preset_id}' to database"
            )

        # Return updated card
        num_webhooks = len(registered_ids)
        return HTMLResponse(
            content=f"""
            <div id="preset-{preset_id}" style="border: 1px solid #e0e0e0; border-radius: 6px; padding: 20px; margin: 15px 0;">
                <h3 style="margin-top: 0; color: #0082c9;">{preset["name"]}</h3>
                <p style="color: #666; margin: 10px 0;">{preset["description"]}</p>
                <p style="font-size: 13px; color: #999;">
                    <strong>App:</strong> {preset["app"]} |
                    <strong>Events:</strong> {len(preset["events"])}
                </p>
                <div style="margin-top: 15px; display: flex; align-items: center; gap: 15px;">
                    <div><span style="color: #4caf50; font-weight: bold;">✓ Enabled ({num_webhooks} webhooks)</span></div>
                    <div>
                        <button
                            hx-delete="/user/webhooks/disable/{preset_id}"
                            hx-target="#preset-{preset_id}"
                            hx-swap="outerHTML"
                            class="button"
                            style="background-color: #ff9800;">
                            Disable
                        </button>
                    </div>
                </div>
            </div>
            """
        )

    except Exception as e:
        logger.error(f"Failed to enable preset {preset_id}: {e}", exc_info=True)
        return HTMLResponse(
            content=f'<div class="warning">Failed to enable preset: {str(e)}</div>',
            status_code=500,
        )


@requires("authenticated", redirect="oauth_login")
async def disable_webhook_preset(request: Request) -> HTMLResponse:
    """Disable a webhook preset by deleting all registered webhooks.

    Args:
        request: Starlette request object (preset_id in path)

    Returns:
        HTML response with updated preset card
    """
    preset_id = request.path_params["preset_id"]

    try:
        # Get authenticated HTTP client
        http_client = await _get_authenticated_client(request)
        username = request.user.display_name

        # Check admin permissions
        is_admin = await is_nextcloud_admin(request, http_client)
        if not is_admin:
            return HTMLResponse(
                content='<div class="warning">Admin access required</div>',
                status_code=403,
            )

        # Get preset configuration
        preset = get_preset(preset_id)
        if not preset:
            return HTMLResponse(
                content=f'<div class="warning">Unknown preset: {preset_id}</div>',
                status_code=404,
            )

        # Find and delete matching webhooks
        webhooks_client = WebhooksClient(http_client, username)

        # Get webhook IDs from database first (more reliable)
        storage = _get_storage(request)
        if storage:
            webhook_ids = await storage.get_webhooks_by_preset(preset_id)
        else:
            # Fallback to API query if storage not available
            enabled_presets = await _get_enabled_presets(webhooks_client)
            webhook_ids = enabled_presets.get(preset_id, [])

        for webhook_id in webhook_ids:
            await webhooks_client.delete_webhook(webhook_id)
            logger.info(f"Deleted webhook {webhook_id} from preset {preset_id}")

        # Remove from database
        if storage:
            deleted_count = await storage.clear_preset_webhooks(preset_id)
            logger.info(
                f"Removed {deleted_count} webhook(s) for preset '{preset_id}' from database"
            )

        # Return updated card
        return HTMLResponse(
            content=f"""
            <div id="preset-{preset_id}" style="border: 1px solid #e0e0e0; border-radius: 6px; padding: 20px; margin: 15px 0;">
                <h3 style="margin-top: 0; color: #0082c9;">{preset["name"]}</h3>
                <p style="color: #666; margin: 10px 0;">{preset["description"]}</p>
                <p style="font-size: 13px; color: #999;">
                    <strong>App:</strong> {preset["app"]} |
                    <strong>Events:</strong> {len(preset["events"])}
                </p>
                <div style="margin-top: 15px; display: flex; align-items: center; gap: 15px;">
                    <div><span style="color: #999;">Not Enabled</span></div>
                    <div>
                        <button
                            hx-post="/user/webhooks/enable/{preset_id}"
                            hx-target="#preset-{preset_id}"
                            hx-swap="outerHTML"
                            class="button button-primary">
                            Enable
                        </button>
                    </div>
                </div>
            </div>
            """
        )

    except Exception as e:
        logger.error(f"Failed to disable preset {preset_id}: {e}", exc_info=True)
        return HTMLResponse(
            content=f'<div class="warning">Failed to disable preset: {str(e)}</div>',
            status_code=500,
        )
