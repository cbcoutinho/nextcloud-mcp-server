"""Helper functions for accessing context in MCP tools."""

import hashlib
import logging
import time
from typing import Any, Protocol, runtime_checkable

from httpx import BasicAuth
from mcp.server.mcpserver import Context

from nextcloud_mcp_server.auth.context_helper import get_client_from_context
from nextcloud_mcp_server.auth.grant_ownership import _ocs_whoami
from nextcloud_mcp_server.auth.scope_authorization import ProvisioningRequiredError
from nextcloud_mcp_server.auth.storage import get_shared_storage
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings

logger = logging.getLogger(__name__)


@runtime_checkable
class BasicAuthLifespanContext(Protocol):
    """Protocol for lifespan contexts that carry a shared NextcloudClient.

    Implemented by :class:`~nextcloud_mcp_server.stdio.StdioContext` and
    the single-user lifespan context in ``app.py``.
    """

    client: NextcloudClient


async def get_client(ctx: Context) -> NextcloudClient:
    """
    Get the appropriate Nextcloud client based on authentication mode.

    Supports the following deployment modes:

    1. BasicAuth mode: Returns shared client from lifespan context
    2. Login Flow v2: OAuth for MCP session, app password for Nextcloud API
    3. Multi-user BasicAuth: Credentials passed through from request headers
       (Basic, or an OAuth Bearer token that Nextcloud validates itself)
    4. OAuth multi-audience: Token contains both MCP and Nextcloud audiences

    This function automatically detects the authentication mode by checking
    the type of the lifespan context.

    Args:
        ctx: MCP request context

    Returns:
        NextcloudClient configured for the current authentication mode

    Raises:
        AttributeError: If context doesn't contain expected data

    Example:
        ```python
        @mcp.tool()
        async def my_tool(ctx: Context):
            client = await get_client(ctx)
            return await client.capabilities()
        ```
    """
    settings = get_settings()

    # Multi-user pass-through mode - extract credentials from request.
    # Basic credentials take the original path; a bearer token is passed
    # through to Nextcloud unchanged.
    if settings.enable_multi_user_basic_auth:
        bearer = _get_request_state(ctx).get("bearer_token")
        if bearer:
            return await _get_client_from_bearer(bearer)
        return _get_client_from_basic_auth(ctx)

    lifespan_ctx: Any = ctx.request_context.lifespan_context

    # Login Flow v2 multi-user mode: app password is REQUIRED for NC API access
    # OAuth token is only used for MCP session identity, not NC API calls
    if hasattr(lifespan_ctx, "nextcloud_host") and settings.enable_login_flow:
        return await _get_client_from_login_flow(ctx, lifespan_ctx.nextcloud_host)

    # BasicAuth mode - use shared client (no token exchange)
    if isinstance(lifespan_ctx, BasicAuthLifespanContext):
        return lifespan_ctx.client

    # OAuth multi-audience mode (has 'nextcloud_host' attribute)
    if hasattr(lifespan_ctx, "nextcloud_host"):
        # Token was validated to have MCP audience in UnifiedTokenVerifier
        # Nextcloud will independently validate its own audience when receiving API calls
        return get_client_from_context(ctx, lifespan_ctx.nextcloud_host)

    # Unknown context type
    raise AttributeError(
        f"Lifespan context does not have 'client' or 'nextcloud_host' attribute. "
        f"Type: {type(lifespan_ctx)}"
    )


def _get_request_state(ctx: Context) -> dict[str, Any]:
    """Request state written by BasicAuthMiddleware, or ``{}`` if absent."""
    request = getattr(ctx.request_context, "request", None)
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return {}
    state = scope.get("state")
    return state if isinstance(state, dict) else {}


# sha256(token) -> (uid, expires_at). Caches only the UID a token belongs to,
# never an authorization decision: Nextcloud re-validates the bearer on every
# API call the client makes, so a revoked token still fails there.
_BEARER_UID_TTL_SECONDS = 60.0
_BEARER_UID_CACHE_MAX = 1024
_bearer_uid_cache: dict[str, tuple[str, float]] = {}


async def _resolve_bearer_uid(token: str) -> str | None:
    """Nextcloud UID a bearer token authenticates as (OCS ``/cloud/user``).

    The UID cannot be read from the token: with an external IdP,
    ``user_oidc`` may map or hash the ``sub``, so Nextcloud is asked directly.
    """
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    cached = _bearer_uid_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]

    uid = await _ocs_whoami(bearer=token)
    if uid:
        if len(_bearer_uid_cache) >= _BEARER_UID_CACHE_MAX:
            for k in [k for k, (_, exp) in _bearer_uid_cache.items() if exp <= now]:
                del _bearer_uid_cache[k]
            if len(_bearer_uid_cache) >= _BEARER_UID_CACHE_MAX:
                _bearer_uid_cache.clear()
        _bearer_uid_cache[key] = (uid, now + _BEARER_UID_TTL_SECONDS)
    return uid


async def _get_client_from_bearer(token: str) -> NextcloudClient:
    """Create a NextcloudClient that passes an OAuth bearer through to Nextcloud.

    For multi-user pass-through mode when the request carries
    ``Authorization: Bearer``. The MCP server does not validate the token;
    Nextcloud does (``user_oidc --check-bearer=1``), first on the
    ``/cloud/user`` lookup that resolves the UID and then on every API call.

    Raises:
        ValueError: If NEXTCLOUD_HOST is not configured, or Nextcloud does not
                   accept the token.
    """
    settings = get_settings()
    if not settings.nextcloud_host:
        raise ValueError(
            "NEXTCLOUD_HOST environment variable must be set for multi-user BasicAuth mode"
        )

    username = await _resolve_bearer_uid(token)
    if not username:
        raise ValueError(
            "Bearer token was not accepted by Nextcloud. "
            "Ensure the token is valid and Nextcloud accepts bearer tokens "
            "from the IdP (user_oidc --check-bearer=1)."
        )

    logger.debug(
        "Creating multi-user bearer pass-through client for %s as %s",
        settings.nextcloud_host,
        username,
    )
    # Same construction the OAuth mode uses (auth/context_helper.py).
    return NextcloudClient.from_token(
        base_url=settings.nextcloud_host, token=token, username=username
    )


def _get_client_from_basic_auth(ctx: Context) -> NextcloudClient:
    """
    Create NextcloudClient from BasicAuth credentials in request headers.

    For multi-user BasicAuth pass-through mode, this function extracts
    username/password from the Authorization: Basic header (stored by
    BasicAuthMiddleware) and creates a client that passes these credentials
    through to Nextcloud APIs.

    The credentials are NOT stored persistently - they exist only for the
    duration of this request (stateless).

    Args:
        ctx: MCP request context with basic_auth in request state

    Returns:
        NextcloudClient configured with BasicAuth credentials

    Raises:
        ValueError: If BasicAuth credentials not found in request or if
                   NEXTCLOUD_HOST is not configured
    """
    settings = get_settings()

    # Validate that NEXTCLOUD_HOST is configured
    if not settings.nextcloud_host:
        raise ValueError(
            "NEXTCLOUD_HOST environment variable must be set for multi-user BasicAuth mode"
        )

    # Extract BasicAuth credentials from request state (set by BasicAuthMiddleware)
    # Access scope through the request object
    scope = getattr(ctx.request_context.request, "scope", None)
    if scope is None:
        raise ValueError("Request scope not available in context")

    request_state = scope.get("state", {})
    basic_auth = request_state.get("basic_auth")

    if not basic_auth:
        raise ValueError(
            "BasicAuth credentials not found in request. "
            "Ensure Authorization: Basic header is provided with valid credentials."
        )

    username = basic_auth.get("username")
    password = basic_auth.get("password")

    if not username or not password:
        raise ValueError("Invalid BasicAuth credentials - missing username or password")

    logger.debug(
        "Creating multi-user BasicAuth client for %s as %s",
        settings.nextcloud_host,
        username,
    )

    # Create client that passes BasicAuth credentials through to Nextcloud
    # settings.nextcloud_host is guaranteed to be str after the check above
    return NextcloudClient(
        base_url=settings.nextcloud_host,
        username=username,
        auth=BasicAuth(username, password),
        password=password,
    )


async def _get_client_from_login_flow(
    ctx: Context, nextcloud_host: str
) -> NextcloudClient:
    """Create NextcloudClient from stored Login Flow v2 app password.

    In Login Flow v2 mode, the OAuth token only provides MCP session identity.
    Nextcloud API calls always use the stored app password obtained via Login Flow v2.

    Args:
        ctx: MCP context (used to extract user identity)
        nextcloud_host: Nextcloud instance URL

    Returns:
        NextcloudClient with stored app password credentials

    Raises:
        ProvisioningRequiredError: If no stored app password exists
    """
    from nextcloud_mcp_server.auth.token_utils import (  # noqa: PLC0415
        extract_user_id_from_token,
    )

    user_id = await extract_user_id_from_token(ctx)
    if not user_id or user_id == "default_user":
        raise ProvisioningRequiredError(
            "Cannot determine user identity from MCP token."
        )

    storage = await get_shared_storage()

    app_data = await storage.get_app_password_with_scopes(user_id)
    if not app_data:
        raise ProvisioningRequiredError(
            "Nextcloud access not provisioned. "
            "Call nc_auth_provision_access to complete Login Flow."
        )

    # ``login_name`` is the Nextcloud loginName returned by Login Flow v2
    # (``app_data["username"]``) — this is the actual Nextcloud username used for
    # both DAV/API path construction (e.g. ``/remote.php/dav/files/<login_name>/``)
    # and app-password authentication.  Falls back to ``user_id`` for legacy rows
    # stored before the loginName column was populated, or when Nextcloud itself is
    # the OIDC IdP and the sub claim equals the NC username.
    #
    # Why ``login_name`` and not ``user_id`` for the DAV path:
    # When an external OIDC provider (e.g. Keycloak) is configured to use
    # ``preferred_username`` as the Nextcloud user identifier, the OIDC ``sub``
    # claim is a UUID (e.g. "a1b2c3d4-…") while the actual Nextcloud username
    # (e.g. "dmartel") is stored as the Login Flow v2 ``loginName``.  Using
    # ``user_id`` (the UUID) as the DAV path segment produces a 404; using
    # ``login_name`` (the NC username) is always correct because Nextcloud itself
    # sets this field during Login Flow v2.
    login_name = app_data.get("username") or user_id
    app_password = app_data["app_password"]

    logger.debug(
        "Creating Login Flow v2 client for %s (id=%s, login=%s)",
        nextcloud_host,
        user_id,
        login_name,
    )

    return NextcloudClient(
        base_url=nextcloud_host,
        username=login_name,
        auth_username=login_name,
        auth=BasicAuth(login_name, app_password),
        password=app_password,
    )
