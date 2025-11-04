"""
MCP Tools for OAuth and Provisioning Management (ADR-004 Progressive Consent).

This module provides MCP tools that enable users to explicitly provision
Nextcloud access using the Flow 2 (Resource Provisioning) OAuth flow.
"""

import logging
import os
import secrets
from typing import Optional
from urllib.parse import urlencode

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage
from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

logger = logging.getLogger(__name__)


class ProvisioningStatus(BaseModel):
    """Status of Nextcloud provisioning for a user."""

    is_provisioned: bool = Field(description="Whether Nextcloud access is provisioned")
    provisioned_at: Optional[str] = Field(
        None, description="ISO timestamp when provisioned"
    )
    client_id: Optional[str] = Field(
        None, description="Client ID that initiated the original Flow 1"
    )
    scopes: Optional[list[str]] = Field(None, description="Granted scopes")
    flow_type: Optional[str] = Field(
        None, description="Type of flow used ('hybrid', 'flow1', 'flow2')"
    )


class ProvisioningResult(BaseModel):
    """Result of provisioning attempt."""

    success: bool = Field(description="Whether provisioning was initiated")
    authorization_url: Optional[str] = Field(
        None, description="URL for user to complete OAuth authorization"
    )
    message: str = Field(description="Status message for the user")
    already_provisioned: bool = Field(
        False, description="Whether access was already provisioned"
    )


class RevocationResult(BaseModel):
    """Result of access revocation."""

    success: bool = Field(description="Whether revocation succeeded")
    message: str = Field(description="Status message for the user")


async def get_provisioning_status(ctx: Context, user_id: str) -> ProvisioningStatus:
    """
    Check the provisioning status for Nextcloud access.

    This checks whether the user has completed Flow 2 to provision
    offline access to Nextcloud resources.

    Args:
        mcp: MCP context
        user_id: User identifier

    Returns:
        ProvisioningStatus with current provisioning state
    """
    storage = RefreshTokenStorage.from_env()
    await storage.initialize()

    token_data = await storage.get_refresh_token(user_id)

    if not token_data:
        return ProvisioningStatus(is_provisioned=False)

    # Convert timestamp to ISO format if present
    provisioned_at_str = None
    if token_data.get("provisioned_at"):
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(token_data["provisioned_at"], tz=timezone.utc)
        provisioned_at_str = dt.isoformat()

    return ProvisioningStatus(
        is_provisioned=True,
        provisioned_at=provisioned_at_str,
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

    This creates the URL that the MCP server uses to get delegated
    access to Nextcloud on behalf of the user.

    Args:
        oidc_discovery_url: OIDC provider discovery URL
        server_client_id: MCP server's OAuth client ID
        redirect_uri: Callback URL for the MCP server
        state: CSRF protection state
        scopes: List of scopes to request

    Returns:
        Complete authorization URL for Flow 2
    """
    # Extract base URL from discovery URL
    # Format: https://example.com/.well-known/openid-configuration
    # We need: https://example.com/apps/oidc/authorize
    base_url = oidc_discovery_url.replace("/.well-known/openid-configuration", "")
    auth_endpoint = f"{base_url}/apps/oidc/authorize"

    # Build OAuth parameters
    params = {
        "response_type": "code",
        "client_id": server_client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        # Request offline access for background operations
        "access_type": "offline",
        "prompt": "consent",  # Force consent screen to show scopes
    }

    return f"{auth_endpoint}?{urlencode(params)}"


async def provision_nextcloud_access(
    ctx: Context, user_id: Optional[str] = None
) -> ProvisioningResult:
    """
    MCP Tool: Provision offline access to Nextcloud resources.

    This tool initiates Flow 2 of the Progressive Consent architecture,
    allowing the MCP server to obtain delegated access to Nextcloud APIs.

    The user must complete the OAuth flow in their browser to grant access.

    Args:
        ctx: MCP context with user's Flow 1 token
        user_id: Optional user identifier (extracted from token if not provided)

    Returns:
        ProvisioningResult with authorization URL or status
    """
    try:
        # Extract user ID from the MCP access token (Flow 1 token)
        if not user_id:
            # Get the authorization token from context
            if hasattr(ctx, "authorization") and ctx.authorization:
                token = ctx.authorization.token
                # Decode token to get user info
                try:
                    import jwt

                    payload = jwt.decode(token, options={"verify_signature": False})
                    user_id = payload.get("sub", "unknown")
                    logger.info(f"Extracted user_id from Flow 1 token: {user_id}")
                except Exception as e:
                    logger.warning(f"Failed to decode token: {e}")
                    user_id = "default_user"
            else:
                user_id = "default_user"

        # Check if already provisioned
        status = await get_provisioning_status(ctx, user_id)
        if status.is_provisioned:
            return ProvisioningResult(
                success=True,
                already_provisioned=True,
                message=(
                    f"Nextcloud access is already provisioned (since {status.provisioned_at}). "
                    "Use 'revoke_nextcloud_access' if you want to re-provision."
                ),
            )

        # Get configuration
        enable_progressive = (
            os.getenv("ENABLE_PROGRESSIVE_CONSENT", "false").lower() == "true"
        )
        if not enable_progressive:
            return ProvisioningResult(
                success=False,
                message=(
                    "Progressive Consent is not enabled. "
                    "Set ENABLE_PROGRESSIVE_CONSENT=true to use this feature."
                ),
            )

        # Get MCP server's OAuth client credentials
        server_client_id = os.getenv("MCP_SERVER_CLIENT_ID")
        if not server_client_id:
            # In production, would use Dynamic Client Registration here
            return ProvisioningResult(
                success=False,
                message=(
                    "MCP server OAuth client not configured. "
                    "Administrator must set MCP_SERVER_CLIENT_ID."
                ),
            )

        # Generate OAuth URL for Flow 2
        oidc_discovery_url = os.getenv(
            "OIDC_DISCOVERY_URL",
            f"{os.getenv('NEXTCLOUD_HOST')}/.well-known/openid-configuration",
        )

        # Generate secure state for CSRF protection
        state = secrets.token_urlsafe(32)

        # Store state in session for validation on callback
        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

        # Create OAuth session for Flow 2
        session_id = f"flow2_{user_id}_{secrets.token_hex(8)}"
        redirect_uri = f"{os.getenv('NEXTCLOUD_MCP_SERVER_URL', 'http://localhost:8000')}/oauth/callback-nextcloud"

        await storage.store_oauth_session(
            session_id=session_id,
            client_redirect_uri="",  # No client redirect for Flow 2
            state=state,
            flow_type="flow2",
            is_provisioning=True,
            ttl_seconds=600,  # 10 minute TTL
        )

        # Define scopes for Nextcloud access
        scopes = [
            "openid",
            "profile",
            "email",
            "offline_access",  # Critical for background operations
            "notes:read",
            "notes:write",
            "calendar:read",
            "calendar:write",
            "contacts:read",
            "contacts:write",
            "files:read",
            "files:write",
        ]

        # Generate authorization URL
        auth_url = generate_oauth_url_for_flow2(
            oidc_discovery_url=oidc_discovery_url,
            server_client_id=server_client_id,
            redirect_uri=redirect_uri,
            state=state,
            scopes=scopes,
        )

        return ProvisioningResult(
            success=True,
            authorization_url=auth_url,
            message=(
                "Please visit the authorization URL to grant the MCP server "
                "offline access to your Nextcloud resources. This is a one-time "
                "setup that allows the server to access Nextcloud on your behalf "
                "even when you're not actively connected."
            ),
        )

    except Exception as e:
        logger.error(f"Failed to initiate provisioning: {e}")
        return ProvisioningResult(
            success=False,
            message=f"Failed to initiate provisioning: {str(e)}",
        )


async def revoke_nextcloud_access(
    ctx: Context, user_id: Optional[str] = None
) -> RevocationResult:
    """
    MCP Tool: Revoke offline access to Nextcloud resources.

    This tool removes the stored refresh token and revokes access
    that was granted via Flow 2.

    Args:
        mcp: MCP context
        user_id: Optional user identifier

    Returns:
        RevocationResult with status
    """
    try:
        # Get user ID from context if not provided
        if not user_id:
            user_id = (
                ctx.context.get("user_id", "default_user")
                if hasattr(ctx, "context")
                else "default_user"
            )

        # Check current status
        status = await get_provisioning_status(ctx, user_id)
        if not status.is_provisioned:
            return RevocationResult(
                success=True,
                message="No Nextcloud access to revoke.",
            )

        # Initialize Token Broker to handle revocation
        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

        encryption_key = os.getenv("TOKEN_ENCRYPTION_KEY")
        if not encryption_key:
            return RevocationResult(
                success=False,
                message="Token encryption key not configured.",
            )

        broker = TokenBrokerService(
            storage=storage,
            oidc_discovery_url=os.getenv(
                "OIDC_DISCOVERY_URL",
                f"{os.getenv('NEXTCLOUD_HOST')}/.well-known/openid-configuration",
            ),
            nextcloud_host=os.getenv("NEXTCLOUD_HOST"),
            encryption_key=encryption_key,
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
        logger.error(f"Failed to revoke access: {e}")
        return RevocationResult(
            success=False,
            message=f"Failed to revoke access: {str(e)}",
        )


async def check_provisioning_status(
    ctx: Context, user_id: Optional[str] = None
) -> ProvisioningStatus:
    """
    MCP Tool: Check the current provisioning status.

    This tool allows users to check whether they have provisioned
    Nextcloud access and see details about their current authorization.

    Args:
        mcp: MCP context
        user_id: Optional user identifier

    Returns:
        ProvisioningStatus with current state
    """
    # Get user ID from context if not provided
    if not user_id:
        user_id = (
            ctx.context.get("user_id", "default_user")
            if hasattr(ctx, "context")
            else "default_user"
        )

    return await get_provisioning_status(ctx, user_id)


# Register MCP tools
def register_oauth_tools(mcp):
    """Register OAuth and provisioning tools with the MCP server."""

    @mcp.tool(
        name="provision_nextcloud_access",
        description=(
            "Provision offline access to Nextcloud resources. "
            "This is required before using Nextcloud tools. "
            "You'll need to complete an OAuth authorization in your browser."
        ),
    )
    @require_scopes("openid")
    async def tool_provision_access(
        ctx: Context,
        user_id: Optional[str] = None,
    ) -> ProvisioningResult:
        return await provision_nextcloud_access(ctx, user_id)

    @mcp.tool(
        name="revoke_nextcloud_access",
        description="Revoke offline access to Nextcloud resources.",
    )
    @require_scopes("openid")
    async def tool_revoke_access(
        ctx: Context, user_id: Optional[str] = None
    ) -> RevocationResult:
        return await revoke_nextcloud_access(ctx, user_id)

    @mcp.tool(
        name="check_provisioning_status",
        description="Check whether Nextcloud access is provisioned.",
    )
    @require_scopes("openid")
    async def tool_check_status(
        ctx: Context, user_id: Optional[str] = None
    ) -> ProvisioningStatus:
        return await check_provisioning_status(ctx, user_id)
