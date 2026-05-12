"""
MCP Tools for OAuth and Provisioning Management (ADR-004 Progressive Consent).

This module provides MCP tools that enable users to explicitly provision
Nextcloud access using the Flow 2 (Resource Provisioning) OAuth flow.
"""

import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.auth.astrolabe_client import AstrolabeClient
from nextcloud_mcp_server.auth.storage import get_shared_storage
from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

# Re-export for backward compatibility — canonical location is auth.token_utils
from nextcloud_mcp_server.auth.token_utils import (
    extract_user_id_from_token as extract_user_id_from_token,  # noqa: PLC0414
)
from nextcloud_mcp_server.config import get_settings

logger = logging.getLogger(__name__)


class ProvisioningStatus(BaseModel):
    """Status of Nextcloud provisioning for a user."""

    is_provisioned: bool = Field(description="Whether Nextcloud access is provisioned")
    provisioned_at: str | None = Field(
        None, description="ISO timestamp when provisioned"
    )
    credential_type: str | None = Field(
        None, description="Type of credential ('refresh_token' or 'app_password')"
    )
    client_id: str | None = Field(
        None, description="Client ID that initiated the original Flow 1"
    )
    scopes: list[str] | None = Field(None, description="Granted scopes")
    flow_type: str | None = Field(
        None, description="Type of flow used ('hybrid', 'flow1', 'flow2')"
    )


class ProvisioningResult(BaseModel):
    """Result of provisioning attempt."""

    success: bool = Field(description="Whether provisioning was initiated")
    provisioning_url: str | None = Field(
        None, description="URL to Astrolabe settings for provisioning background sync"
    )
    message: str = Field(description="Status message for the user")
    already_provisioned: bool = Field(
        False, description="Whether access was already provisioned"
    )


class RevocationResult(BaseModel):
    """Result of access revocation."""

    success: bool = Field(description="Whether revocation succeeded")
    message: str = Field(description="Status message for the user")


class LoginConfirmation(BaseModel):
    """Schema for login confirmation elicitation."""

    acknowledged: bool = Field(
        default=False,
        description="Check this box after completing login at the provided URL",
    )


async def _get_provisioning_status(ctx: Context, user_id: str) -> ProvisioningStatus:
    """
    Check the provisioning status for Nextcloud access.

    Internal helper — leading underscore signals that ``user_id`` is a
    trusted identity claim that callers MUST derive from the verified
    access token. The MCP tool wrappers in ``register_oauth_tools`` are
    the only legitimate callers (PR #758 round-3 finding 3).

    Checks for both credential types:
    1. App password from Astrolabe (works today)
    2. OAuth refresh token from storage (for future)

    Args:
        mcp: MCP context
        user_id: User identifier

    Returns:
        ProvisioningStatus with current provisioning state
    """
    settings = get_settings()

    # Check for app password first (interim solution)
    if settings.oidc_client_id and settings.oidc_client_secret:
        try:
            astrolabe = AstrolabeClient(
                nextcloud_host=settings.nextcloud_host or "",
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret,
            )
            status = await astrolabe.get_background_sync_status(user_id)

            if status.get("has_access"):
                # Demoted to debug (PR #758 round-2 nit 4): user_id ends up
                # in log aggregation on every call, which is noise in a
                # multi-tenant deployment.
                logger.debug(
                    "  get_provisioning_status: app password FOUND for user_id=%s",
                    user_id,
                )
                provisioned_at_str = status.get("provisioned_at")
                return ProvisioningStatus(
                    is_provisioned=True,
                    provisioned_at=provisioned_at_str,
                    credential_type="app_password",
                )
        except Exception as e:
            logger.debug("  App password check failed for %s: %s", user_id, e)

    # Check for OAuth refresh token (fallback)
    logger.debug(
        "  get_provisioning_status: looking up refresh token for user_id=%s", user_id
    )
    storage = await get_shared_storage()

    token_data = await storage.get_refresh_token(user_id)

    if not token_data:
        logger.debug(
            "  get_provisioning_status: no credentials found for user_id=%s", user_id
        )
        return ProvisioningStatus(is_provisioned=False)

    logger.debug(
        "  get_provisioning_status: refresh token FOUND for user_id=%s "
        "flow_type=%s provisioning_client_id=%s",
        user_id,
        token_data.get("flow_type"),
        token_data.get("provisioning_client_id", "N/A"),
    )

    # Convert timestamp to ISO format if present
    provisioned_at_str = None
    if token_data.get("provisioned_at"):
        dt = datetime.fromtimestamp(token_data["provisioned_at"], tz=timezone.utc)
        provisioned_at_str = dt.isoformat()

    return ProvisioningStatus(
        is_provisioned=True,
        provisioned_at=provisioned_at_str,
        credential_type="refresh_token",
        client_id=token_data.get("provisioning_client_id"),
        scopes=token_data.get("scopes"),
        flow_type=token_data.get("flow_type", "hybrid"),
    )


def generate_oauth_url_for_flow2(
    oidc_discovery_url: str,
    server_client_id: str,
    redirect_uri: str,
    state: str,
    scopes: list[str],
) -> str:
    """
    Generate OAuth authorization URL for Flow 2 (Resource Provisioning).

    This returns the MCP server's Flow 2 authorization endpoint, which will:
    1. Generate PKCE parameters (required by Nextcloud OIDC)
    2. Store code_verifier in session
    3. Redirect to Nextcloud IdP with PKCE
    4. Handle the callback with code_verifier for token exchange

    Args:
        oidc_discovery_url: OIDC provider discovery URL (unused, kept for compatibility)
        server_client_id: MCP server's OAuth client ID (unused, kept for compatibility)
        redirect_uri: Callback URL for the MCP server (unused, kept for compatibility)
        state: CSRF protection state
        scopes: List of scopes to request (unused, kept for compatibility)

    Returns:
        MCP server's Flow 2 authorization URL with state parameter
    """
    # Use the MCP server's Flow 2 endpoint which handles PKCE internally
    # This endpoint will:
    # - Generate code_verifier and code_challenge (PKCE)
    # - Store code_verifier in session storage
    # - Redirect to Nextcloud with PKCE parameters
    # - Handle the callback with proper code_verifier
    mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")
    auth_endpoint = f"{mcp_server_url}/oauth/authorize-nextcloud"

    # Only pass state parameter - the endpoint handles everything else
    params = {"state": state}

    return f"{auth_endpoint}?{urlencode(params)}"


async def _provision_nextcloud_access(ctx: Context, user_id: str) -> ProvisioningResult:
    """
    Internal helper for the ``provision_nextcloud_access`` MCP tool.

    Returns URL to Astrolabe settings page where users can provision background
    sync access using either:
    - App password (works today, interim solution)
    - OAuth refresh token (future, when Nextcloud supports OAuth for app APIs)

    Args:
        ctx: MCP context with user's Flow 1 token
        user_id: Authenticated user identifier (must be derived from the
            verified access token by the caller; never accept from MCP input).

    Returns:
        ProvisioningResult with Astrolabe settings URL or status
    """
    try:
        # Check if already provisioned
        status = await _get_provisioning_status(ctx, user_id)
        if status.is_provisioned:
            return ProvisioningResult(
                success=True,
                already_provisioned=True,
                message=(
                    f"Nextcloud access is already provisioned (credential_type={status.credential_type}, "
                    f"since {status.provisioned_at}). "
                    "Use 'revoke_nextcloud_access' if you want to re-provision."
                ),
            )

        # Get configuration using settings (handles both ENABLE_BACKGROUND_OPERATIONS
        # and ENABLE_OFFLINE_ACCESS environment variables)
        settings = get_settings()
        if not settings.enable_offline_access:
            return ProvisioningResult(
                success=False,
                message=(
                    "Offline access is not enabled. "
                    "Set ENABLE_BACKGROUND_OPERATIONS=true to use this feature."
                ),
            )

        # Return Astrolabe settings URL for background sync provisioning
        nextcloud_host = os.getenv("NEXTCLOUD_HOST", "http://localhost:8080")
        astrolabe_url = f"{nextcloud_host}/settings/user/astrolabe#background-sync"

        return ProvisioningResult(
            success=True,
            provisioning_url=astrolabe_url,
            message=(
                "Visit Astrolabe settings to provision background sync access.\n\n"
                "You can choose either:\n"
                "- App password (works today, recommended for now)\n"
                "- OAuth refresh token (future, when Nextcloud fully supports OAuth)\n\n"
                "After provisioning, background sync will enable the MCP server to "
                "access Nextcloud resources even when you're not actively connected."
            ),
        )

    except Exception as e:
        logger.error("Failed to initiate provisioning: %s", e)
        return ProvisioningResult(
            success=False,
            message=f"Failed to initiate provisioning: {str(e)}",
        )


async def _revoke_nextcloud_access(ctx: Context, user_id: str) -> RevocationResult:
    """
    Internal helper for the ``revoke_nextcloud_access`` MCP tool.

    This tool removes the stored refresh token and revokes access
    that was granted via Flow 2.

    Args:
        ctx: MCP context
        user_id: Authenticated user identifier (must be derived from the
            verified access token by the caller; never accept from MCP input).

    Returns:
        RevocationResult with status
    """
    try:
        # Check current status
        status = await _get_provisioning_status(ctx, user_id)
        if not status.is_provisioned:
            return RevocationResult(
                success=True,
                message="No Nextcloud access to revoke.",
            )

        # Initialize Token Broker to handle revocation
        storage = await get_shared_storage()

        # Get OAuth client credentials from storage
        client_creds = await storage.get_oauth_client()
        if not client_creds:
            return RevocationResult(
                success=False,
                message="OAuth client credentials not found in storage.",
            )

        broker = TokenBrokerService(
            storage=storage,
            oidc_discovery_url=os.getenv(
                "OIDC_DISCOVERY_URL",
                f"{os.getenv('NEXTCLOUD_HOST')}/.well-known/openid-configuration",
            ),
            nextcloud_host=os.getenv("NEXTCLOUD_HOST"),  # type: ignore
            client_id=client_creds["client_id"],
            client_secret=client_creds["client_secret"],
        )

        # Revoke access
        success = await broker.revoke_nextcloud_access(user_id)

        if success:
            return RevocationResult(
                success=True,
                message=(
                    "Successfully revoked Nextcloud access. "
                    "You can run 'provision_nextcloud_access' again if needed."
                ),
            )
        else:
            return RevocationResult(
                success=False,
                message="Failed to revoke access. Please try again.",
            )

    except Exception as e:
        logger.error("Failed to revoke access: %s", e)
        return RevocationResult(
            success=False,
            message=f"Failed to revoke access: {str(e)}",
        )


async def _check_provisioning_status(ctx: Context, user_id: str) -> ProvisioningStatus:
    """
    Internal helper for the ``check_provisioning_status`` MCP tool.

    This tool allows users to check whether they have provisioned
    Nextcloud access and see details about their current authorization.

    Args:
        ctx: MCP context
        user_id: Authenticated user identifier (must be derived from the
            verified access token by the caller; never accept from MCP input).

    Returns:
        ProvisioningStatus with current state
    """
    return await _get_provisioning_status(ctx, user_id)


async def _check_logged_in(ctx: Context, user_id: str) -> str:
    """
    Internal helper for the ``check_logged_in`` MCP tool.

    This tool checks whether the user has completed Flow 2 (resource provisioning)
    to grant offline access to Nextcloud. If not logged in, it uses MCP elicitation
    to prompt the user to complete the login flow.

    Args:
        ctx: MCP context with user's Flow 1 token
        user_id: Authenticated user identifier (must be derived from the
            verified access token by the caller; never accept from MCP input).

    Returns:
        "yes" if logged in, or elicitation prompting for login
    """
    try:
        # Demoted to debug (PR #758 round-2 nit 4): per-user logging at INFO
        # ends up in log aggregation on every check_logged_in call, which is
        # noise in a hosted multi-tenant deployment.
        logger.debug("Checking provisioning status for user_id=%s", user_id)
        status = await _get_provisioning_status(ctx, user_id)
        logger.debug(
            "  Provisioning status for %s: is_provisioned=%s",
            user_id,
            status.is_provisioned,
        )

        if status.is_provisioned:
            logger.debug("User %s already logged in", user_id)
            return "yes"

        logger.debug("User %s NOT logged in — triggering elicitation", user_id)

        # Not logged in - generate OAuth URL for Flow 2
        # Use settings (handles both ENABLE_BACKGROUND_OPERATIONS and ENABLE_OFFLINE_ACCESS)
        settings = get_settings()
        if not settings.enable_offline_access:
            return (
                "Not logged in. Offline access is not enabled. "
                "Set ENABLE_BACKGROUND_OPERATIONS=true to use this feature."
            )

        # Get MCP server's OAuth client credentials
        # Try environment variable first, then fall back to DCR client_id
        server_client_id = os.getenv("MCP_SERVER_CLIENT_ID")
        if not server_client_id:
            # Try to get from lifespan context (DCR)
            lifespan_ctx = ctx.request_context.lifespan_context
            if hasattr(lifespan_ctx, "server_client_id"):
                server_client_id = lifespan_ctx.server_client_id

        if not server_client_id:
            return (
                "Not logged in. MCP server OAuth client not configured. "
                "Set MCP_SERVER_CLIENT_ID environment variable or use Dynamic Client Registration."
            )

        # Generate OAuth URL for Flow 2
        oidc_discovery_url = os.getenv(
            "OIDC_DISCOVERY_URL",
            f"{os.getenv('NEXTCLOUD_HOST')}/.well-known/openid-configuration",
        )

        # Generate secure state for CSRF protection
        state = secrets.token_urlsafe(32)

        # Store state in session for validation on callback
        storage = await get_shared_storage()

        # The canonical Flow 2 oauth_session row is written inside
        # generate_oauth_url_for_flow2 (keyed by `state`, with the PKCE
        # verifier and nonce); the unified callback looks it up by `state`.
        # No additional row is needed here.
        redirect_uri = f"{os.getenv('NEXTCLOUD_MCP_SERVER_URL', 'http://localhost:8000')}/oauth/callback"

        # Define scopes for Nextcloud access
        # Note: offline_access is only included when enabled in settings.
        # The actual scope sent to the IdP is determined by
        # oauth_authorize_nextcloud() based on IdP discovery, so this list
        # is informational (generate_oauth_url_for_flow2 marks it as unused).
        scopes = [
            "openid",
            "profile",
            "email",
            "notes.read",
            "notes.write",
            "calendar.read",
            "calendar.write",
            "contacts.read",
            "contacts.write",
            "files.read",
            "files.write",
        ]
        if get_settings().enable_offline_access:
            scopes.insert(3, "offline_access")

        # Generate authorization URL
        auth_url = generate_oauth_url_for_flow2(
            oidc_discovery_url=oidc_discovery_url,
            server_client_id=server_client_id,
            redirect_uri=redirect_uri,
            state=state,
            scopes=scopes,
        )

        # Use elicitation to prompt user to login. Logged at debug (PR #758
        # round-2 nit 4): the auth URL contains the per-request ``state``
        # token, which is sensitive enough that it shouldn't land in
        # multi-tenant log aggregation by default.
        logger.debug("Eliciting login for user %s (URL omitted)", user_id)

        result = await ctx.elicit(
            message=f"Please log in to Nextcloud at the following URL:\n\n{auth_url}\n\nAfter completing the login, check the box below and click OK.",
            schema=LoginConfirmation,
        )

        if result.action == "accept":
            # Check if login was successful by looking for refresh token
            # Strategy: Try multiple lookup methods to handle both flows.
            # Demoted to debug (PR #758 round-2 nit 4): user_id + state
            # appear here on every elicitation accept.
            logger.debug(
                "User accepted login prompt; looking up refresh token "
                "(user_id=%s state=%s...)",
                user_id,
                state[:16],
            )

            # First, try to find token by provisioning_client_id (Flow 2 from elicitation)
            refresh_token_data = (
                await storage.get_refresh_token_by_provisioning_client_id(state)
            )

            if refresh_token_data:
                logger.debug(
                    "Refresh token found via provisioning_client_id lookup "
                    "(flow_type=%s provisioned_at=%s)",
                    refresh_token_data.get("flow_type", "unknown"),
                    refresh_token_data.get("provisioned_at", "unknown"),
                )
                return "yes"

            # Fallback: Try to find token by user_id (browser login or any other flow)
            logger.debug(
                "No token via provisioning_client_id=%s...; falling back to user_id=%s",
                state[:16],
                user_id,
            )

            refresh_token_data = await storage.get_refresh_token(user_id)

            if refresh_token_data:
                logger.debug(
                    "Refresh token found via user_id lookup "
                    "(flow_type=%s provisioned_at=%s provisioning_client_id=%s)",
                    refresh_token_data.get("flow_type", "unknown"),
                    refresh_token_data.get("provisioned_at", "unknown"),
                    refresh_token_data.get("provisioning_client_id", "NULL"),
                )
                return "yes"

            # No token found by either method
            logger.warning(
                "No refresh token found for user_id=%s (checked provisioning_client_id=%s... and user_id) — "
                "user completed elicitation but token wasn't stored",
                user_id,
                state[:16],
            )

            return (
                "Login not detected. Please ensure you completed the login "
                "at the provided URL before clicking OK."
            )
        elif result.action == "decline":
            return "Login declined by user."
        else:
            return "Login cancelled by user."

    except Exception as e:
        logger.error("Failed to check login status: %s", e)
        return f"Error checking login status: {str(e)}"


# Register MCP tools
def register_oauth_tools(mcp):
    """Register OAuth and provisioning tools with the MCP server."""

    @mcp.tool(
        name="provision_nextcloud_access",
        title="Grant Server Access to Nextcloud",
        description=(
            "Provision offline access to Nextcloud resources. "
            "This is required before using Nextcloud tools. "
            "You'll need to complete an OAuth authorization in your browser."
        ),
        annotations=ToolAnnotations(
            idempotentHint=False,  # Creates new OAuth session each time
            openWorldHint=True,
        ),
    )
    @require_scopes("openid")
    async def tool_provision_access(ctx: Context) -> ProvisioningResult:
        user_id = await extract_user_id_from_token(ctx)
        return await _provision_nextcloud_access(ctx, user_id)

    @mcp.tool(
        name="revoke_nextcloud_access",
        title="Revoke Server Access to Nextcloud",
        description="Revoke offline access to Nextcloud resources.",
        annotations=ToolAnnotations(
            destructiveHint=True,  # Removes stored access tokens
            idempotentHint=True,  # Revoking revoked access = same end state
            openWorldHint=True,
        ),
    )
    @require_scopes("openid")
    async def tool_revoke_access(ctx: Context) -> RevocationResult:
        user_id = await extract_user_id_from_token(ctx)
        return await _revoke_nextcloud_access(ctx, user_id)

    @mcp.tool(
        name="check_provisioning_status",
        title="Check Provisioning Status",
        description="Check whether Nextcloud access is provisioned.",
        annotations=ToolAnnotations(
            readOnlyHint=True,  # Only checks status, doesn't modify
            openWorldHint=True,
        ),
    )
    @require_scopes("openid")
    async def tool_check_status(ctx: Context) -> ProvisioningStatus:
        user_id = await extract_user_id_from_token(ctx)
        return await _check_provisioning_status(ctx, user_id)

    @mcp.tool(
        name="check_logged_in",
        title="Check Server Login Status",
        description=(
            "Check if you are logged in to Nextcloud. "
            "If not logged in, this tool will prompt you to complete the login flow."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,  # Checking status doesn't modify state
            openWorldHint=True,
        ),
    )
    @require_scopes("openid")
    async def tool_check_logged_in(ctx: Context) -> str:
        user_id = await extract_user_id_from_token(ctx)
        return await _check_logged_in(ctx, user_id)
