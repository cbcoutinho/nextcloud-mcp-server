"""MCP tools for Login Flow v2 authentication (ADR-022).

Provides tools for users to provision Nextcloud access via Login Flow v2,
check provisioning status, and update granted scopes.

These tools work alongside (not replacing) the existing OAuth provisioning
tools during the migration period.
"""

import logging

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth.elicitation import present_login_url
from nextcloud_mcp_server.auth.login_flow import LoginFlowV2Client
from nextcloud_mcp_server.auth.scope_authorization import (
    invalidate_scope_cache,
    require_scopes,
)
from nextcloud_mcp_server.auth.storage import get_shared_storage
from nextcloud_mcp_server.auth.token_utils import extract_user_id_from_token
from nextcloud_mcp_server.config import get_nextcloud_ssl_verify, get_settings
from nextcloud_mcp_server.models.auth import (
    ALL_SUPPORTED_SCOPES,
    ProvisionAccessResponse,
    ProvisionStatusResponse,
    UpdateScopesResponse,
)

logger = logging.getLogger(__name__)


def register_auth_tools(mcp: FastMCP) -> None:
    """Register Login Flow v2 auth tools with the MCP server."""

    @mcp.tool(
        name="nc_auth_provision_access",
        title="Provision Nextcloud Access",
        description=(
            "Start Nextcloud Login Flow v2 to obtain an app password. "
            "This is required before using any Nextcloud tools. "
            "You will be given a URL to open in your browser to log in."
        ),
        annotations=ToolAnnotations(
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @require_scopes("openid")
    async def nc_auth_provision_access(
        ctx: Context,
        scopes: list[str] | None = None,
    ) -> ProvisionAccessResponse:
        """Provision Nextcloud access via Login Flow v2.

        Args:
            ctx: MCP context
            scopes: Requested application scopes (e.g. ["notes.read", "calendar.write"]).
                    If not specified, all available scopes are requested.

        Returns:
            ProvisionAccessResponse with login URL or status
        """
        user_id = await extract_user_id_from_token(ctx)
        if user_id == "default_user":
            return ProvisionAccessResponse(
                status="error",
                message="Could not determine user identity from MCP token.",
                success=False,
            )

        storage = await get_shared_storage()

        # Check if already provisioned
        existing = await storage.get_app_password_with_scopes(user_id)
        if existing:
            return ProvisionAccessResponse(
                status="already_provisioned",
                message=(
                    f"Nextcloud access already provisioned for {user_id}. "
                    f"Scopes: {existing['scopes'] or 'all'}. "
                    f"Use nc_auth_update_scopes to modify permissions."
                ),
                user_id=user_id,
                requested_scopes=existing["scopes"],
            )

        # Determine scopes
        requested_scopes = scopes if scopes else list(ALL_SUPPORTED_SCOPES)

        # Validate requested scopes
        invalid_scopes = [s for s in requested_scopes if s not in ALL_SUPPORTED_SCOPES]
        if invalid_scopes:
            return ProvisionAccessResponse(
                status="error",
                message=f"Invalid scopes: {', '.join(invalid_scopes)}. "
                f"Valid scopes: {', '.join(sorted(ALL_SUPPORTED_SCOPES))}",
                success=False,
            )

        # Initiate Login Flow v2
        settings = get_settings()
        nextcloud_host = settings.nextcloud_host
        if not nextcloud_host:
            return ProvisionAccessResponse(
                status="error",
                message="NEXTCLOUD_HOST not configured on the server.",
                success=False,
            )

        try:
            flow_client = LoginFlowV2Client(
                nextcloud_host=nextcloud_host,
                verify_ssl=get_nextcloud_ssl_verify(),
            )
            init_response = await flow_client.initiate()
        except Exception as e:
            logger.error(f"Failed to initiate Login Flow v2: {e}")
            return ProvisionAccessResponse(
                status="error",
                message=f"Failed to start login flow: {e}",
                success=False,
            )

        # Store the polling session
        await storage.store_login_flow_session(
            user_id=user_id,
            poll_token=init_response.poll_token,
            poll_endpoint=init_response.poll_endpoint,
            requested_scopes=requested_scopes,
        )

        # Present login URL to user via elicitation
        elicitation_result = await present_login_url(ctx, init_response.login_url)

        if elicitation_result == "declined":
            await storage.delete_login_flow_session(user_id)
            return ProvisionAccessResponse(
                status="declined",
                message="Login flow declined. Call nc_auth_provision_access again to retry.",
                user_id=user_id,
                success=False,
            )

        if elicitation_result == "cancelled":
            await storage.delete_login_flow_session(user_id)
            return ProvisionAccessResponse(
                status="cancelled",
                message="Login flow cancelled. Call nc_auth_provision_access again to retry.",
                user_id=user_id,
                success=False,
            )

        message = (
            f"Please open this URL in your browser to log in to Nextcloud:\n\n"
            f"{init_response.login_url}\n\n"
            f"After logging in, call nc_auth_check_status to complete provisioning."
        )

        if elicitation_result == "accepted":
            message = (
                "Login acknowledged. Call nc_auth_check_status to verify "
                "and complete provisioning."
            )
            return ProvisionAccessResponse(
                status="pending",
                login_url=init_response.login_url,
                message=message,
                user_id=user_id,
                requested_scopes=requested_scopes,
            )

        return ProvisionAccessResponse(
            status="login_required",
            login_url=init_response.login_url,
            message=message,
            user_id=user_id,
            requested_scopes=requested_scopes,
        )

    @mcp.tool(
        name="nc_auth_check_status",
        title="Check Nextcloud Access Status",
        description=(
            "Check if Nextcloud access has been provisioned. "
            "If a Login Flow is pending, this will poll for completion. "
            "Recommended polling interval: 5 seconds."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    @require_scopes("openid")
    async def nc_auth_check_status(
        ctx: Context,
    ) -> ProvisionStatusResponse:
        """Check provisioning status and poll pending Login Flows.

        Returns:
            ProvisionStatusResponse with current status
        """
        user_id = await extract_user_id_from_token(ctx)
        if user_id == "default_user":
            return ProvisionStatusResponse(
                status="error",
                message="Could not determine user identity from MCP token.",
                success=False,
            )

        storage = await get_shared_storage()

        # Check for existing app password
        existing = await storage.get_app_password_with_scopes(user_id)
        if existing:
            return ProvisionStatusResponse(
                status="provisioned",
                message=f"Nextcloud access is provisioned for {existing.get('username') or user_id}.",
                user_id=user_id,
                scopes=existing["scopes"],
                username=existing.get("username"),
            )

        # Check for pending login flow session
        try:
            session = await storage.get_login_flow_session(user_id)
        except Exception as e:
            logger.error(f"Failed to check login flow session for {user_id}: {e}")
            return ProvisionStatusResponse(
                status="error",
                message=f"Failed to check login flow session: {e}",
                user_id=user_id,
                success=False,
            )
        if not session:
            return ProvisionStatusResponse(
                status="not_initiated",
                message=(
                    "No provisioning in progress. "
                    "Call nc_auth_provision_access to start."
                ),
                user_id=user_id,
            )

        # Poll the Login Flow
        settings = get_settings()
        nextcloud_host = settings.nextcloud_host
        if not nextcloud_host:
            return ProvisionStatusResponse(
                status="error",
                message="NEXTCLOUD_HOST not configured.",
                success=False,
            )

        try:
            flow_client = LoginFlowV2Client(
                nextcloud_host=nextcloud_host,
                verify_ssl=get_nextcloud_ssl_verify(),
            )
            poll_result = await flow_client.poll(
                poll_endpoint=session["poll_endpoint"],
                poll_token=session["poll_token"],
            )
        except Exception as e:
            logger.error(f"Failed to poll Login Flow v2: {e}")
            return ProvisionStatusResponse(
                status="error",
                message=f"Failed to check login status: {e}",
                success=False,
            )

        if poll_result.status == "completed":
            # Store the app password with scopes
            if poll_result.app_password is None:
                return ProvisionStatusResponse(
                    status="error",
                    message="Login Flow completed but no app password was returned.",
                    success=False,
                )
            await storage.store_app_password_with_scopes(
                user_id=user_id,
                app_password=poll_result.app_password,
                scopes=session.get("requested_scopes"),
                username=poll_result.login_name,
            )
            invalidate_scope_cache(user_id)

            # Clean up the flow session
            await storage.delete_login_flow_session(user_id)

            return ProvisionStatusResponse(
                status="provisioned",
                message=f"Nextcloud access provisioned successfully as {poll_result.login_name}.",
                user_id=user_id,
                scopes=session.get("requested_scopes"),
                username=poll_result.login_name,
            )

        if poll_result.status == "expired":
            # Clean up expired session
            await storage.delete_login_flow_session(user_id)
            return ProvisionStatusResponse(
                status="not_initiated",
                message=(
                    "Login flow expired. "
                    "Call nc_auth_provision_access to start a new one."
                ),
                user_id=user_id,
            )

        # Still pending
        return ProvisionStatusResponse(
            status="pending",
            message=(
                "Login flow is still pending. "
                "Please complete the login in your browser, then call this tool again."
            ),
            user_id=user_id,
        )

    @mcp.tool(
        name="nc_auth_update_scopes",
        title="Update Nextcloud Access Scopes",
        description=(
            "Update the scopes for your Nextcloud access. "
            "This starts a new Login Flow with the combined scope set. "
            "The current app password remains valid until the new one is obtained."
        ),
        annotations=ToolAnnotations(
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @require_scopes("openid")
    async def nc_auth_update_scopes(
        ctx: Context,
        add_scopes: list[str] | None = None,
        remove_scopes: list[str] | None = None,
    ) -> UpdateScopesResponse:
        """Update granted scopes by re-provisioning with merged scope set.

        Args:
            ctx: MCP context
            add_scopes: Scopes to add to the current set
            remove_scopes: Scopes to remove from the current set

        Returns:
            UpdateScopesResponse with new login URL or status
        """
        user_id = await extract_user_id_from_token(ctx)
        if user_id == "default_user":
            return UpdateScopesResponse(
                status="error",
                message="Could not determine user identity from MCP token.",
                success=False,
            )

        if not add_scopes and not remove_scopes:
            return UpdateScopesResponse(
                status="error",
                message="Provide add_scopes and/or remove_scopes to update.",
                success=False,
            )

        storage = await get_shared_storage()

        # Get current state - require existing provisioning
        existing = await storage.get_app_password_with_scopes(user_id)
        if existing is None:
            return UpdateScopesResponse(
                status="error",
                message="Not provisioned. Call nc_auth_provision_access first.",
                success=False,
            )

        previous_scopes = existing["scopes"]

        # Compute new scope set
        current_set = (
            set(previous_scopes) if previous_scopes else set(ALL_SUPPORTED_SCOPES)
        )
        if add_scopes:
            invalid = [s for s in add_scopes if s not in ALL_SUPPORTED_SCOPES]
            if invalid:
                return UpdateScopesResponse(
                    status="error",
                    message=f"Invalid scopes: {', '.join(invalid)}",
                    success=False,
                )
            current_set.update(add_scopes)
        if remove_scopes:
            current_set -= set(remove_scopes)

        new_scopes = sorted(current_set)

        if not new_scopes:
            return UpdateScopesResponse(
                status="error",
                message="Cannot remove all scopes. At least one scope must remain.",
                success=False,
            )

        # No-op detection: skip Login Flow if scopes are unchanged
        previous_scopes_set = (
            set(previous_scopes) if previous_scopes else set(ALL_SUPPORTED_SCOPES)
        )
        if set(new_scopes) == previous_scopes_set:
            return UpdateScopesResponse(
                status="unchanged",
                message="Requested scopes match current scopes. No changes needed.",
                previous_scopes=previous_scopes,
                new_scopes=new_scopes,
            )

        # Initiate new Login Flow v2
        # Note: existing app password stays valid until the new flow completes.
        # store_app_password_with_scopes() does an upsert, so the old password
        # is replaced atomically when nc_auth_check_status stores the new one.
        settings = get_settings()
        nextcloud_host = settings.nextcloud_host
        if not nextcloud_host:
            return UpdateScopesResponse(
                status="error",
                message="NEXTCLOUD_HOST not configured.",
                success=False,
            )

        try:
            flow_client = LoginFlowV2Client(
                nextcloud_host=nextcloud_host,
                verify_ssl=get_nextcloud_ssl_verify(),
            )
            init_response = await flow_client.initiate()
        except Exception as e:
            logger.error(f"Failed to initiate Login Flow v2 for scope update: {e}")
            return UpdateScopesResponse(
                status="error",
                message=f"Failed to start re-provisioning flow: {e}",
                success=False,
            )

        # Store new flow session
        await storage.store_login_flow_session(
            user_id=user_id,
            poll_token=init_response.poll_token,
            poll_endpoint=init_response.poll_endpoint,
            requested_scopes=new_scopes,
        )

        # Present login URL
        elicitation_result = await present_login_url(ctx, init_response.login_url)

        if elicitation_result == "declined":
            await storage.delete_login_flow_session(user_id)
            return UpdateScopesResponse(
                status="declined",
                message="Scope update declined. Call nc_auth_update_scopes again to retry.",
                previous_scopes=previous_scopes if previous_scopes else None,
                new_scopes=new_scopes,
                success=False,
            )

        if elicitation_result == "cancelled":
            await storage.delete_login_flow_session(user_id)
            return UpdateScopesResponse(
                status="cancelled",
                message="Scope update cancelled. Call nc_auth_update_scopes again to retry.",
                previous_scopes=previous_scopes if previous_scopes else None,
                new_scopes=new_scopes,
                success=False,
            )

        message = (
            f"Scope update requires re-authentication.\n\n"
            f"Please open this URL to log in:\n{init_response.login_url}\n\n"
            f"After logging in, call nc_auth_check_status to complete."
        )

        if elicitation_result == "accepted":
            message = (
                "Login acknowledged for scope update. "
                "Call nc_auth_check_status to verify and complete."
            )

        return UpdateScopesResponse(
            status="login_required",
            login_url=init_response.login_url,
            message=message,
            previous_scopes=previous_scopes if previous_scopes else None,
            new_scopes=new_scopes,
        )
