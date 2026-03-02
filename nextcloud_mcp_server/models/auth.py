"""Pydantic response models for Login Flow v2 auth tools."""

from pydantic import Field

from nextcloud_mcp_server.models.base import BaseResponse


class ProvisionAccessResponse(BaseResponse):
    """Response from nc_auth_provision_access tool."""

    status: str = Field(
        description="Provisioning status: 'login_required', 'already_provisioned', 'declined', 'cancelled', 'error'"
    )
    login_url: str | None = Field(
        None, description="URL to open in browser for Nextcloud login"
    )
    message: str = Field(description="Human-readable status message")
    user_id: str | None = Field(None, description="MCP user ID")
    requested_scopes: list[str] | None = Field(
        None, description="Scopes requested in this provisioning flow"
    )


class ProvisionStatusResponse(BaseResponse):
    """Response from nc_auth_check_status tool."""

    status: str = Field(
        description="Status: 'provisioned', 'pending', 'not_initiated', 'error'"
    )
    message: str = Field(description="Human-readable status message")
    user_id: str | None = Field(None, description="MCP user ID")
    scopes: list[str] | None = Field(
        None, description="Granted scopes (None = all scopes)"
    )
    username: str | None = Field(None, description="Nextcloud username (loginName)")


class UpdateScopesResponse(BaseResponse):
    """Response from nc_auth_update_scopes tool."""

    status: str = Field(
        description="Status: 'login_required', 'unchanged', 'declined', 'cancelled', 'error'"
    )
    login_url: str | None = Field(
        None, description="URL for re-provisioning with new scopes"
    )
    message: str = Field(description="Human-readable status message")
    previous_scopes: list[str] | None = Field(
        None, description="Previously granted scopes"
    )
    new_scopes: list[str] | None = Field(None, description="Updated scope set")


# All supported application-level scopes
ALL_SUPPORTED_SCOPES = (
    "notes:read",
    "notes:write",
    "calendar:read",
    "calendar:write",
    "todo:read",
    "todo:write",
    "contacts:read",
    "contacts:write",
    "files:read",
    "files:write",
    "tables:read",
    "tables:write",
    "deck:read",
    "deck:write",
    "cookbook:read",
    "cookbook:write",
    "sharing:read",
    "sharing:write",
    "news:read",
    "news:write",
)
