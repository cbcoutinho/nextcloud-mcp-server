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

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage
from nextcloud_mcp_server.auth.token_broker import TokenBrokerService
from nextcloud_mcp_server.auth.userinfo_routes import _query_idp_userinfo

logger = logging.getLogger(__name__)


async def extract_user_id_from_token(ctx: Context) -> str:
    """Extract user_id from the MCP access token (Flow 1).

    Handles both JWT and opaque tokens:
    - JWT: Decode and extract 'sub' claim
    - Opaque: Call userinfo endpoint to get 'sub'

    Args:
        ctx: MCP context with access token

    Returns:
        user_id extracted from token, or "default_user" as fallback
    """
    # Use MCP SDK's get_access_token() which uses contextvars
    access_token: AccessToken | None = get_access_token()

    if not access_token or not access_token.token:
        logger.warning("  ✗ No access token found via get_access_token()")
        return "default_user"

    token = access_token.token
    is_jwt = "." in token and token.count(".") >= 2
    logger.info(f"  Token type: {'JWT' if is_jwt else 'Opaque'}")

    # Try JWT decode first
    if is_jwt:
        try:
            import jwt

            payload = jwt.decode(token, options={"verify_signature": False})
            user_id = payload.get("sub", "unknown")
            logger.info(f"  ✓ JWT decode successful: user_id={user_id}")
            return user_id
        except Exception as e:
            logger.error(f"  ✗ JWT decode failed: {type(e).__name__}: {e}")

    # Opaque token - call userinfo endpoint
    logger.info("  Opaque token detected, calling userinfo endpoint...")
    try:
        # Get userinfo endpoint from OIDC discovery
        oidc_discovery_uri = os.getenv(
            "OIDC_DISCOVERY_URI",
            "http://localhost:8080/.well-known/openid-configuration",
        )
        async with httpx.AsyncClient() as http_client:
            discovery_response = await http_client.get(oidc_discovery_uri)
            discovery_response.raise_for_status()
            discovery = discovery_response.json()
            userinfo_endpoint = discovery.get("userinfo_endpoint")

        if userinfo_endpoint:
            userinfo = await _query_idp_userinfo(token, userinfo_endpoint)
            if userinfo:
                user_id = userinfo.get("sub", "unknown")
                logger.info(f"  ✓ Userinfo query successful: user_id={user_id}")
                return user_id
            else:
                logger.error("  ✗ Userinfo query failed")
        else:
            logger.error("  ✗ No userinfo_endpoint available")
    except Exception as e:
        logger.error(f"  ✗ Userinfo query failed: {type(e).__name__}: {e}")

    # Fallback
    logger.warning("  Using fallback user_id: default_user")
    return "default_user"


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


class LoginConfirmation(BaseModel):
    """Schema for login confirmation elicitation."""

    acknowledged: bool = Field(
        default=False,
        description="Check this box after completing login at the provided URL",
    )


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
    logger.info(
        f"  get_provisioning_status: Looking up refresh token for user_id={user_id}"
    )
    storage = RefreshTokenStorage.from_env()
    await storage.initialize()

    token_data = await storage.get_refresh_token(user_id)

    if not token_data:
        logger.info(
            f"  get_provisioning_status: ✗ No refresh token found for user_id={user_id}"
        )
        return ProvisioningStatus(is_provisioned=False)

    logger.info(
        f"  get_provisioning_status: ✓ Refresh token FOUND for user_id={user_id}"
    )
    logger.info(f"    flow_type: {token_data.get('flow_type')}")
    logger.info(
        f"    provisioning_client_id: {token_data.get('provisioning_client_id', 'N/A')}"
    )

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
                token = ctx.authorization.token  # type: ignore
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
        enable_offline_access = (
            os.getenv("ENABLE_OFFLINE_ACCESS", "false").lower() == "true"
        )
        if not enable_offline_access:
            return ProvisioningResult(
                success=False,
                message=(
                    "Offline access is not enabled. "
                    "Set ENABLE_OFFLINE_ACCESS=true to use this feature."
                ),
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
            return ProvisioningResult(
                success=False,
                message=(
                    "MCP server OAuth client not configured. "
                    "Set MCP_SERVER_CLIENT_ID environment variable or use Dynamic Client Registration."
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
        redirect_uri = f"{os.getenv('NEXTCLOUD_MCP_SERVER_URL', 'http://localhost:8000')}/oauth/callback"

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
        # Get user ID from token if not provided
        if not user_id:
            logger.info("Extracting user_id from access token for revoke...")
            user_id = await extract_user_id_from_token(ctx)
            logger.info(f"  Revoke using user_id: {user_id}")

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
            nextcloud_host=os.getenv("NEXTCLOUD_HOST"),  # type: ignore
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
            ctx.context.get("user_id", "default_user")  # type: ignore
            if hasattr(ctx, "context")
            else "default_user"
        )

    return await get_provisioning_status(ctx, user_id)


async def check_logged_in(ctx: Context, user_id: Optional[str] = None) -> str:
    """
    MCP Tool: Check if user is logged in and elicit login if needed.

    This tool checks whether the user has completed Flow 2 (resource provisioning)
    to grant offline access to Nextcloud. If not logged in, it uses MCP elicitation
    to prompt the user to complete the login flow.

    Args:
        ctx: MCP context with user's Flow 1 token
        user_id: Optional user identifier (extracted from token if not provided)

    Returns:
        "yes" if logged in, or elicitation prompting for login
    """
    try:
        # Extract user ID from the MCP access token (Flow 1 token)
        logger.info("=" * 60)
        logger.info("check_logged_in: Starting user_id extraction")
        logger.info("=" * 60)

        if not user_id:
            user_id = await extract_user_id_from_token(ctx)
            logger.info(f"  Final user_id for check_logged_in: {user_id}")
        else:
            logger.info(f"  user_id provided as argument: {user_id}")

        # Check if already logged in
        logger.info(f"Checking provisioning status for user_id: {user_id}")
        status = await get_provisioning_status(ctx, user_id)
        logger.info(f"  Provisioning status: is_provisioned={status.is_provisioned}")

        if status.is_provisioned:
            logger.info(f"✓ User {user_id} is already logged in - returning 'yes'")
            logger.info("=" * 60)
            return "yes"

        logger.info(f"✗ User {user_id} is NOT logged in - triggering elicitation")
        logger.info("=" * 60)

        # Not logged in - generate OAuth URL for Flow 2
        enable_offline_access = (
            os.getenv("ENABLE_OFFLINE_ACCESS", "false").lower() == "true"
        )
        if not enable_offline_access:
            return (
                "Not logged in. Offline access is not enabled. "
                "Set ENABLE_OFFLINE_ACCESS=true to use this feature."
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
        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

        # Create OAuth session for Flow 2
        session_id = f"flow2_{user_id}_{secrets.token_hex(8)}"
        redirect_uri = f"{os.getenv('NEXTCLOUD_MCP_SERVER_URL', 'http://localhost:8000')}/oauth/callback"

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

        # Use elicitation to prompt user to login
        logger.info(f"Eliciting login for user {user_id} with URL: {auth_url}")

        result = await ctx.elicit(
            message=f"Please log in to Nextcloud at the following URL:\n\n{auth_url}\n\nAfter completing the login, check the box below and click OK.",
            schema=LoginConfirmation,
        )

        if result.action == "accept":
            # Check if login was successful by looking for refresh token
            # Strategy: Try multiple lookup methods to handle both flows
            logger.info("User accepted login prompt, checking for refresh token")
            logger.info(f"  State parameter: {state[:16]}...")
            logger.info(f"  User ID: {user_id}")

            # First, try to find token by provisioning_client_id (Flow 2 from elicitation)
            refresh_token_data = (
                await storage.get_refresh_token_by_provisioning_client_id(state)
            )

            if refresh_token_data:
                logger.info("✓ Refresh token found via provisioning_client_id lookup")
                logger.info(
                    f"  Flow type: {refresh_token_data.get('flow_type', 'unknown')}"
                )
                logger.info(
                    f"  Provisioned at: {refresh_token_data.get('provisioned_at', 'unknown')}"
                )
                return "yes"

            # Fallback: Try to find token by user_id (browser login or any other flow)
            logger.info(f"✗ No token found with provisioning_client_id={state[:16]}...")
            logger.info(f"  Trying fallback lookup by user_id: {user_id}")

            refresh_token_data = await storage.get_refresh_token(user_id)

            if refresh_token_data:
                logger.info("✓ Refresh token found via user_id lookup")
                logger.info(
                    f"  Flow type: {refresh_token_data.get('flow_type', 'unknown')}"
                )
                logger.info(
                    f"  Provisioned at: {refresh_token_data.get('provisioned_at', 'unknown')}"
                )
                logger.info(
                    f"  Provisioning client ID: {refresh_token_data.get('provisioning_client_id', 'NULL')}"
                )
                logger.info(
                    "  Note: This token was created via browser login or different flow"
                )
                return "yes"

            # No token found by either method
            logger.warning(f"✗ No refresh token found for user {user_id}")
            logger.warning(
                f"  Checked provisioning_client_id={state[:16]}... - NOT FOUND"
            )
            logger.warning(f"  Checked user_id={user_id} - NOT FOUND")
            logger.warning(
                "  This may indicate the user completed login but token wasn't stored"
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
        logger.error(f"Failed to check login status: {e}")
        return f"Error checking login status: {str(e)}"


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

    @mcp.tool(
        name="check_logged_in",
        description=(
            "Check if you are logged in to Nextcloud. "
            "If not logged in, this tool will prompt you to complete the login flow."
        ),
    )
    @require_scopes("openid")
    async def tool_check_logged_in(ctx: Context, user_id: Optional[str] = None) -> str:
        return await check_logged_in(ctx, user_id)
