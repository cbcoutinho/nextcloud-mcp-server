"""Webhook management routes for admin UI.

Provides browser-based endpoints for admin users to manage webhook configurations
using preset templates. Only accessible to Nextcloud administrators.
"""

import html
import logging
import os

import httpx
from starlette.authentication import requires
from starlette.requests import Request
from starlette.responses import HTMLResponse

from nextcloud_mcp_server.auth.permissions import is_nextcloud_admin
from nextcloud_mcp_server.client.webhooks import WebhooksClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.server.webhook_presets import (
    WEBHOOK_PRESETS,
    WebhookPreset,
    filter_presets_by_installed_apps,
    get_preset,
)

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


def _get_storage(request: Request):
    """Get storage instance from app state.

    Args:
        request: Starlette request object

    Returns:
        RefreshTokenStorage instance or None
    """
    # Try browser_app state first (for /app routes)
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
        logger.warning("Failed to get installed apps from capabilities: %s", e)
        return []


def _get_webhook_uri() -> str:
    """Get the webhook endpoint URI for this MCP server.

    Priority (highest first):
      1. ``WEBHOOK_INTERNAL_URL`` — explicit override, e.g. for split
         internal/external URLs (read via dynaconf, so env vars and
         settings.toml both work).
      2. ``NEXTCLOUD_MCP_SERVER_URL`` — the configured public URL set on
         cloud deployments (ECS, k8s); the URL NC must POST to.
      3. ``/.dockerenv`` (or podman / ``DOCKER_CONTAINER=true``) → internal
         docker-compose service name. Only relevant when no public URL is
         configured — i.e. local dev where MCP and NC share a Docker
         network.
      4. ``http://localhost:8000`` — last-resort fallback.

    Note: ECS Fargate containers also expose ``/.dockerenv``. Without this
    priority order, cloud deployments would silently register an internal
    docker-compose hostname (e.g. ``http://mcp:8000``) that NC cannot
    resolve, dropping every webhook delivery.
    """
    settings = get_settings()
    if settings.webhook_internal_url:
        return f"{settings.webhook_internal_url}/webhooks/nextcloud"

    if settings.nextcloud_mcp_server_url:
        return f"{settings.nextcloud_mcp_server_url}/webhooks/nextcloud"

    # Docker-environment markers stay on os.getenv: they're container-runtime
    # signals (filesystem markers, optional service-name override) rather
    # than user-facing config that would belong in settings.toml.
    is_docker = (
        os.path.exists("/.dockerenv")
        or os.path.exists("/run/.containerenv")
        or os.getenv("DOCKER_CONTAINER") == "true"
    )
    if is_docker:
        service_name = os.getenv("NEXTCLOUD_MCP_SERVICE_NAME", "mcp")
        port = os.getenv("NEXTCLOUD_MCP_PORT", "8000")
        logger.debug(
            "Docker environment detected, using internal URL: http://%s:%s",
            service_name,
            port,
        )
        return f"http://{service_name}:{port}/webhooks/nextcloud"

    return "http://localhost:8000/webhooks/nextcloud"


class WebhookSecretNotConfigured(RuntimeError):
    """Raised when a webhook registration is attempted without WEBHOOK_SECRET.

    Webhooks require ``WEBHOOK_SECRET`` (GHSA-8vh3-g2qg-2h2c): the receiver
    route is not mounted without it, so registering a webhook would only create
    an unauthenticated delivery target pointing at a non-existent endpoint.
    """


def webhook_auth_pair() -> tuple[str, dict[str, str]]:
    """Resolve ``(auth_method, auth_data)`` for new webhook registrations.

    Returns ``("header", {"Authorization": f"Bearer {secret}"})`` so NC stores
    the credential encrypted at-rest and forwards it on every delivery.

    ``WEBHOOK_SECRET`` is required (GHSA-8vh3-g2qg-2h2c): the receiver refuses
    unauthenticated deliveries and ``app.py`` does not mount the route without
    a secret, so registering an ``authMethod="none"`` webhook would only create
    a dead, unauthenticated delivery target. Callers must surface
    :class:`WebhookSecretNotConfigured` as a clear operator-facing error.

    Shared by both registration call sites: the ``/app/webhooks`` preset
    flow and the Astrolabe-facing ``/api/v1/webhooks`` endpoint.

    Raises:
        WebhookSecretNotConfigured: when ``WEBHOOK_SECRET`` is unset.
    """
    secret = get_settings().webhook_secret
    if not secret:
        raise WebhookSecretNotConfigured(
            "WEBHOOK_SECRET must be set to register webhooks. Without it the "
            "/webhooks/nextcloud receiver is disabled and any registration "
            "would point at a non-existent, unauthenticated endpoint."
        )
    return ("header", {"Authorization": f"Bearer {secret}"})


async def _register_preset_webhooks(
    webhooks_client: WebhooksClient,
    preset: WebhookPreset,
    webhook_uri: str,
) -> list[int]:
    """Register every event in a preset against a single MCP webhook URI.

    Threads the resolved ``(auth_method, auth_data)`` from
    :func:`webhook_auth_pair` onto each registration call so deliveries carry
    the configured ``Authorization`` header. ``WEBHOOK_SECRET`` is required
    (GHSA-8vh3-g2qg-2h2c): with no secret this raises
    :class:`WebhookSecretNotConfigured` before any webhook is created.

    Extracted from :func:`enable_webhook_preset` so the auth-threading
    behaviour is testable without standing up a Starlette app.

    Raises:
        WebhookSecretNotConfigured: when ``WEBHOOK_SECRET`` is unset.
    """
    auth_method, auth_data = webhook_auth_pair()
    registered_ids: list[int] = []
    for event_config in preset["events"]:
        webhook_data = await webhooks_client.create_webhook(
            event=event_config["event"],
            uri=webhook_uri,
            event_filter=event_config["filter"] if event_config["filter"] else None,
            auth_method=auth_method,
            auth_data=auth_data,
        )
        webhook_id = webhook_data["id"]
        registered_ids.append(webhook_id)
        logger.info("Registered webhook %s for %s", webhook_id, event_config["event"])
    return registered_ids


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
        assert username is not None and password is not None  # Type narrowing
        return nextcloud_httpx_client(
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

    return nextcloud_httpx_client(
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
        logger.error("Failed to list webhooks: %s", e)
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
        logger.debug("Installed apps: %s", installed_apps)

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
                    hx-delete="/app/webhooks/disable/{preset_id}"
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
                    hx-post="/app/webhooks/enable/{preset_id}"
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
            <p><strong>Endpoint:</strong> <code>{html.escape(webhook_uri)}</code></p>
        </div>

        <h3 style="margin-top: 30px;">Available Presets</h3>
        <p style="color: #666;">Enable webhook presets with one click for common synchronization scenarios.</p>
        <p style="color: #999; font-size: 13px; margin-top: 5px;">Showing {len(available_presets)} preset(s) for your installed apps ({len(installed_apps)} detected)</p>

        {preset_cards_html}
        """

        return HTMLResponse(content=html_content)

    except Exception as e:
        logger.error("Error loading webhook management pane: %s", e, exc_info=True)
        return HTMLResponse(
            content=f"""
            <div class="warning">
                <p><strong>Error Loading Webhooks</strong></p>
                <p>{html.escape(str(e))}</p>
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
                content=f'<div class="warning">Unknown preset: {html.escape(preset_id)}</div>',
                status_code=404,
            )

        # Register webhooks
        webhooks_client = WebhooksClient(http_client, username)
        webhook_uri = _get_webhook_uri()
        registered_ids = await _register_preset_webhooks(
            webhooks_client, preset, webhook_uri
        )

        # Persist webhook IDs to database
        storage = _get_storage(request)
        if storage:
            for webhook_id in registered_ids:
                await storage.store_webhook(webhook_id, preset_id)
            logger.info(
                "Persisted %d webhook(s) for preset '%s' to database",
                len(registered_ids),
                preset_id,
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
                            hx-delete="/app/webhooks/disable/{preset_id}"
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

    except WebhookSecretNotConfigured as e:
        logger.warning("Refusing to enable preset %s: %s", preset_id, e)
        return HTMLResponse(
            content=(
                '<div class="warning">Webhooks are disabled: WEBHOOK_SECRET is '
                "not set. Configure it and restart the server to enable webhook "
                "presets.</div>"
            ),
            status_code=503,
        )
    except Exception as e:
        logger.error("Failed to enable preset %s: %s", preset_id, e, exc_info=True)
        return HTMLResponse(
            content=f'<div class="warning">Failed to enable preset: {html.escape(str(e))}</div>',
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
                content=f'<div class="warning">Unknown preset: {html.escape(preset_id)}</div>',
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
            logger.info("Deleted webhook %s from preset %s", webhook_id, preset_id)

        # Remove from database
        if storage:
            deleted_count = await storage.clear_preset_webhooks(preset_id)
            logger.info(
                "Removed %d webhook(s) for preset '%s' from database",
                deleted_count,
                preset_id,
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
                            hx-post="/app/webhooks/enable/{preset_id}"
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
        logger.error("Failed to disable preset %s: %s", preset_id, e, exc_info=True)
        return HTMLResponse(
            content=f'<div class="warning">Failed to disable preset: {html.escape(str(e))}</div>',
            status_code=500,
        )
