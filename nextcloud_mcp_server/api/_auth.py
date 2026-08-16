"""Credential resolution helpers for non-MCP-Context API endpoints.

Starlette routes (e.g. ``api/apps.py``) authenticate the inbound request
via OAuth bearer validation, then need to make Nextcloud API calls on behalf
of that user. Per ADR-022 / ``docs/login-flow-v2.md`` the data leg to
Nextcloud always uses **HTTP Basic Auth with the user's app password**, never
the OAuth token. This module resolves that credential pair.

The MCP tool path uses ``context.get_client(ctx)``; this is the equivalent
helper for callers that have a validated ``user_id`` but no MCP ``Context``.
"""

from nextcloud_mcp_server.auth.scope_authorization import ProvisioningRequiredError
from nextcloud_mcp_server.auth.storage import get_shared_storage


async def get_basic_auth_for_user(user_id: str) -> tuple[str, str]:
    """Resolve ``(username, app_password)`` for an OAuth-validated user.

    Reads the per-user app password provisioned via Login Flow v2 from
    encrypted SQLite storage. The username returned is the actual Nextcloud
    username recorded at provisioning time (which may differ from the IdP
    user-id when an external IdP is configured).

    Args:
        user_id: MCP user identifier extracted from a validated OAuth token.

    Returns:
        Tuple ``(username, app_password)`` ready to pass to
        ``httpx.BasicAuth``.

    Raises:
        ProvisioningRequiredError: No app password is stored for ``user_id``.
            The caller should surface this to the client so the user can
            complete Login Flow v2 (typically via ``nc_auth_provision_access``
            or the Astrolabe settings page).
    """
    storage = await get_shared_storage()
    app_data = await storage.get_app_password_with_scopes(user_id)
    if not app_data:
        raise ProvisioningRequiredError(
            f"No Nextcloud app password provisioned for user {user_id!r}. "
            "Complete Login Flow v2 (nc_auth_provision_access) before "
            "calling this endpoint."
        )

    username = app_data.get("username") or user_id
    return username, app_data["app_password"]
