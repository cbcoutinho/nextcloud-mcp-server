"""Management API endpoints for Nextcloud PHP app integration.

ADR-018: Provides REST API endpoints for the Nextcloud PHP app to query:
- Server status and version
- User session information and background access status
- Vector sync metrics

All endpoints use OAuth bearer token authentication via UnifiedTokenVerifier.
The PHP app obtains tokens through PKCE flow and uses them to access these endpoints.

Shared helper functions for other API modules are also exported from here:
- extract_bearer_token: Extract OAuth token from request
- validate_token_and_get_user: Validate token and get user ID
- _sanitize_error_for_client: Return safe error messages
- _parse_int_param, _parse_float_param, _validate_query_string: Parameter validation
"""

import logging
import time
from importlib.metadata import version
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.config import Settings, get_settings
from nextcloud_mcp_server.config_validators import AuthMode, detect_auth_mode
from nextcloud_mcp_server.vector.metrics_publisher import count_indexed
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


# Get package version from metadata
__version__ = version("nextcloud-mcp-server")

# Track server start time for uptime calculation
_server_start_time = time.time()

# The search algorithms astrolabe's McpServerClient understands (the ``algorithm``
# request param to /api/v1/search and /api/v1/vector-viz/search): ``semantic``
# (dense only), ``bm25`` (sparse/keyword only), ``hybrid`` (dense+sparse fusion).
# Single source of truth, also consumed by api/visualization.py for validation.
SUPPORTED_SEARCH_ALGORITHMS: tuple[str, ...] = ("semantic", "bm25", "hybrid")


def supported_search_types(settings: Settings) -> list[str]:
    """Search algorithms this server can actually serve, given its config.

    Advertised via /api/v1/status (ADR-030) so the astrolabe UI can gate which
    query types it offers, without having to know the server's SEARCH_MODE:

    - vector sync disabled → ``[]`` (no Qdrant-backed search at all)
    - SEARCH_MODE=keyword  → ``["bm25"]`` (dense embeddings are off, so
      ``semantic`` and the dense half of ``hybrid`` are unavailable)
    - SEARCH_MODE=hybrid   → all three (``semantic``, ``bm25``, ``hybrid``)

    Order follows ``SUPPORTED_SEARCH_ALGORITHMS`` for a stable contract.
    """
    if not settings.vector_sync_enabled:
        return []
    if settings.dense_enabled:
        return list(SUPPORTED_SEARCH_ALGORITHMS)
    return ["bm25"]


def resolve_search_algorithm(algorithm: str, settings: Settings) -> str:
    """Coerce a requested search algorithm to one this server can serve now.

    Falls back to a supported algorithm when the request is unknown OR
    unavailable in the current SEARCH_MODE (ADR-030) — notably ``semantic`` while
    ``SEARCH_MODE=keyword``, which would otherwise route a dense query at a
    sparse-only index and fail. Prefers ``hybrid`` when available (the historical
    default), else the first supported type. Keeps the search endpoints lenient
    (no new 4xx) while keeping the accepted set in lockstep with the
    ``supported_search_types`` that /api/v1/status advertises.
    """
    supported = supported_search_types(settings)
    if algorithm in supported:
        return algorithm
    # Empty (vector sync off) preserves the prior default of "hybrid".
    resolved = "hybrid" if "hybrid" in supported else (supported or ["hybrid"])[0]
    # Log the rewrite so operators debugging "why did I get bm25 results for an
    # algorithm=semantic request" can see the coercion (the response
    # search_method reflects the algorithm actually run, not the request).
    logger.debug(
        "resolve_search_algorithm: %r unavailable in current SEARCH_MODE "
        "(supported=%r); coercing to %r",
        algorithm,
        supported,
        resolved,
    )
    return resolved


def extract_bearer_token(request: Request) -> str | None:
    """Extract OAuth bearer token from Authorization header.

    Args:
        request: Starlette request

    Returns:
        Token string or None if no valid Authorization header
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None

    # Parse "Bearer <token>"
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    return parts[1]


async def validate_token_and_get_user(
    request: Request,
) -> tuple[str, dict[str, Any]]:
    """Validate OAuth bearer token and extract user ID.

    Uses verify_token_for_management_api which accepts any valid Nextcloud OIDC
    token (not just MCP-audience tokens). This is needed because Astrolabe
    (NC PHP app) uses its own OAuth client, separate from MCP server's client.

    Security Model:
    ~~~~~~~~~~~~~~~
    - **Authentication** (this function): Verifies token is cryptographically valid
      and extracts user identity from the `sub` claim.
    - **Authorization** (calling endpoints): Each endpoint MUST verify that the
      authenticated user owns the requested resource. For example:
      - GET /users/{user_id}/session: Checks token_user_id == path_user_id (403 if mismatch)
      - POST /users/{user_id}/revoke: Checks token_user_id == path_user_id (403 if mismatch)

    This separation ensures that even without audience validation, users can only
    access their own resources. Cross-user access is blocked at the authorization layer.

    Args:
        request: Starlette request with Authorization header

    Returns:
        Tuple of (user_id, validated_token_data)

    Raises:
        Exception: If token is invalid or missing
    """
    token = extract_bearer_token(request)
    if not token:
        raise ValueError("Missing Authorization header")

    # Get token verifier from app state
    # Note: This is set in app.py starlette_lifespan for OAuth mode
    token_verifier = request.app.state.oauth_context["token_verifier"]

    # Validate token for management API (handles both JWT and opaque tokens)
    # Uses verify_token_for_management_api which accepts any valid Nextcloud token
    # without requiring MCP audience - needed for Astrolabe integration (ADR-018)
    access_token = await token_verifier.verify_token_for_management_api(token)

    if not access_token:
        raise ValueError("Token validation failed")

    # Extract user ID from AccessToken.resource field (set during verification)
    user_id = access_token.resource
    if not user_id:
        raise ValueError("Token missing user identifier")

    # Return user_id and a dict with token info for compatibility
    validated = {
        "sub": user_id,
        "client_id": access_token.client_id,
        "scopes": access_token.scopes,
        "expires_at": access_token.expires_at,
    }

    return user_id, validated


class AdminScopeRequired(Exception):
    """Raised when an authenticated caller lacks the ``admin`` scope."""


async def require_admin_scope(request: Request) -> str:
    """Authenticate the caller and require the ``admin`` scope.

    This is the first ``/api/v1/admin/*`` guard; it establishes the pattern
    (auth via :func:`validate_token_and_get_user`, then an explicit scope check).
    Raises ``ValueError`` on auth failure and :class:`AdminScopeRequired` on a
    valid token that lacks ``admin``; callers map these to 401 / 403.
    """
    user_id, validated = await validate_token_and_get_user(request)
    scopes = validated.get("scopes") or []
    if "admin" not in scopes:
        raise AdminScopeRequired("admin scope required")
    return user_id


def _sanitize_error_for_client(error: Exception, context: str = "") -> str:
    """
    Return a safe, generic error message for clients.

    Detailed error is logged internally but not exposed to clients to prevent
    information leakage (database paths, API URLs, tokens, etc.).

    Args:
        error: The exception that occurred
        context: Optional context for logging (e.g., "revoke_user_access")

    Returns:
        Generic error message safe for client consumption
    """
    # Log detailed error for debugging
    logger.error("Error in %s: %s", context, error, exc_info=True)

    # Return generic message
    return "An internal error occurred. Please contact your administrator."


def _parse_int_param(
    value: str | None,
    default: int,
    min_val: int,
    max_val: int,
    param_name: str,
) -> int:
    """Parse and validate integer parameter."""
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(f"Invalid {param_name}: must be an integer")
    if parsed < min_val or parsed > max_val:
        raise ValueError(
            f"Invalid {param_name}: must be between {min_val} and {max_val}"
        )
    return parsed


def _parse_float_param(
    value: Any,
    default: float,
    min_val: float,
    max_val: float,
    param_name: str,
) -> float:
    """Parse and validate float parameter."""
    if value is None:
        return default
    try:
        parsed = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid {param_name}: must be a number")
    if parsed < min_val or parsed > max_val:
        raise ValueError(
            f"Invalid {param_name}: must be between {min_val} and {max_val}"
        )
    return parsed


def _validate_query_string(query: str, max_length: int = 10000) -> None:
    """Validate query string length."""
    if len(query) > max_length:
        raise ValueError(f"Query too long: maximum {max_length} characters")


async def get_server_status(request: Request) -> JSONResponse:
    """GET /api/v1/status - Server status and version.

    Returns basic server information including version, auth mode,
    vector sync status, and uptime.

    Public endpoint - no authentication required.
    """
    # Public endpoint - no authentication required

    # Get configuration
    settings = get_settings()

    # Calculate uptime
    uptime_seconds = int(time.time() - _server_start_time)

    # Determine auth mode using proper mode detection
    mode = detect_auth_mode(settings)

    # Map deployment mode to auth_mode for API response
    # This helps clients (like Astrolabe) determine which auth flow to use
    if mode == AuthMode.LOGIN_FLOW:
        auth_mode = "oauth"
    elif mode == AuthMode.MULTI_USER_BASIC:
        auth_mode = "multi_user_basic"
    elif mode == AuthMode.SINGLE_USER_BASIC:
        auth_mode = "basic"
    else:
        auth_mode = "unknown"

    response_data = {
        "version": __version__,
        "auth_mode": auth_mode,
        "vector_sync_enabled": settings.vector_sync_enabled,
        # Whether the /webhooks/nextcloud receiver is active. Gated on
        # WEBHOOK_SECRET (GHSA-8vh3-g2qg-2h2c): without a secret the route is
        # not mounted, so the Astrolabe UI can show webhooks as unavailable and
        # vector sync falls back to the polling scanner.
        "webhooks_enabled": bool(settings.webhook_secret),
        # Query types the astrolabe UI may offer for this server (ADR-030).
        # Empty when vector sync is off; ["bm25"] in keyword mode; all three in
        # hybrid mode. Lets the UI gate its algorithm picker without knowing
        # SEARCH_MODE.
        "supported_search_types": supported_search_types(settings),
        "uptime_seconds": uptime_seconds,
        "management_api_version": "1.0",
    }

    # Add app password support indicator for multi-user BasicAuth mode
    if mode == AuthMode.MULTI_USER_BASIC:
        response_data["supports_app_passwords"] = settings.enable_offline_access

    # Include OIDC configuration if OAuth is available
    # This includes OAuth mode AND hybrid mode (multi_user_basic + offline_access)
    # Astrolabe needs OIDC config to discover IdP for OAuth flow in hybrid mode
    oauth_provisioning_available = auth_mode == "oauth" or (
        mode == AuthMode.MULTI_USER_BASIC and settings.enable_offline_access
    )
    if oauth_provisioning_available:
        # Provide IdP discovery information for NC PHP app
        oidc_config = {}

        if settings.oidc_discovery_url:
            oidc_config["discovery_url"] = settings.oidc_discovery_url

        if settings.oidc_issuer:
            oidc_config["issuer"] = settings.oidc_issuer

        if oidc_config:
            response_data["oidc"] = oidc_config

    return JSONResponse(response_data)


async def get_vector_sync_status(request: Request) -> JSONResponse:
    """GET /api/v1/vector-sync/status - Vector sync metrics.

    Returns real-time indexing status and metrics.

    Requires: VECTOR_SYNC_ENABLED=true

    Public endpoint - no authentication required.
    """
    # Public endpoint - no authentication required

    settings = get_settings()
    if not settings.vector_sync_enabled:
        return JSONResponse(
            {"error": "Vector sync is disabled on this server"},
            status_code=404,
        )

    try:
        # Outstanding-work view depends on the queue backend (Deck #183):
        # memory → stream buffer depth; postgres → procrastinate job counts.
        from nextcloud_mcp_server.vector.ingest_status import (  # noqa: PLC0415
            get_ingest_pending,
        )

        pending = await get_ingest_pending(
            task_producer=getattr(request.app.state, "task_producer", None),
            document_receive_stream=getattr(
                request.app.state, "document_receive_stream", None
            ),
            ingest_queue=settings.ingest_queue,
        )

        # Corpus size (backend-independent): distinct documents AND total
        # chunks. A single "indexed" figure is ambiguous because each document
        # fans out to ~N chunks, so both are reported (the UI shows both).
        indexed_documents = 0
        indexed_chunks = 0
        try:
            qdrant_client = await get_qdrant_client()
            indexed_documents, indexed_chunks = await count_indexed(
                qdrant_client, settings.get_collection_name()
            )
        except Exception as e:
            logger.warning("Failed to query Qdrant for indexed counts: %s", e)
            # Continue with zeroed counts

        # Determine status
        status = "syncing" if pending.pending > 0 else "idle"

        body: dict[str, object] = {
            "status": status,
            # indexed_documents is now the distinct-document count (was the chunk
            # count before — the two differ by the per-document chunk fan-out).
            # indexed_chunks exposes the raw point count separately; indexed_count
            # is kept as a deprecated alias of indexed_chunks for back-compat.
            "indexed_documents": indexed_documents,
            "indexed_chunks": indexed_chunks,
            "indexed_count": indexed_chunks,
            "pending_documents": pending.pending,
            "ingest_queue": settings.ingest_queue,
        }
        if pending.job_counts is not None:
            # Per-status breakdown (todo/doing/failed/…) on the postgres backend.
            body["job_counts"] = pending.job_counts
        if pending.job_counts_by_queue is not None:
            # Per-tier-queue breakdown (Deck #323): where work sits across the
            # ingest-fast / ingest-structured / ingest-ocr fleets.
            body["job_counts_by_queue"] = pending.job_counts_by_queue
        return JSONResponse(body)

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "get_vector_sync_status")
        return JSONResponse(
            {"error": error_msg},
            status_code=500,
        )


async def get_user_session(request: Request) -> JSONResponse:
    """GET /api/v1/users/{user_id}/session - User session details.

    Returns information about the user's MCP session including:
    - Background access status (offline_access)
    - IdP profile information

    Requires OAuth bearer token. The user_id in the path must match
    the user_id in the token.
    """
    try:
        # Validate OAuth token and extract user
        token_user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "get_user_session_auth")
        return JSONResponse(
            {"error": error_msg},
            status_code=401,
        )

    # Get user_id from path
    path_user_id = request.path_params.get("user_id")

    # Verify token user matches requested user
    if token_user_id != path_user_id:
        logger.warning(
            "User %s attempted to access session for %s", token_user_id, path_user_id
        )
        return JSONResponse(
            {
                "error": "Forbidden",
                "message": "Cannot access another user's session",
            },
            status_code=403,
        )

    # Check if offline access is enabled
    # Use settings.enable_offline_access which handles both ENABLE_BACKGROUND_OPERATIONS (new)
    # and ENABLE_OFFLINE_ACCESS (deprecated) environment variables
    settings = get_settings()
    enable_offline_access = settings.enable_offline_access

    if not enable_offline_access:
        # Offline access disabled - return minimal session info
        return JSONResponse(
            {
                "session_id": token_user_id,
                "background_access_granted": False,
            }
        )

    # Get refresh token storage from app state
    storage = request.app.state.oauth_context.get("storage")
    if not storage:
        logger.error("Refresh token storage not available in app state")
        return JSONResponse(
            {
                "session_id": token_user_id,
                "background_access_granted": False,
                "error": "Storage not configured",
            }
        )

    try:
        # Check if user has refresh token stored
        refresh_token_data = await storage.get_refresh_token(token_user_id)

        if not refresh_token_data:
            # No refresh token - user hasn't provisioned background access
            return JSONResponse(
                {
                    "session_id": token_user_id,
                    "background_access_granted": False,
                }
            )

        # User has background access - get profile info
        profile = await storage.get_user_profile(token_user_id)

        response_data = {
            "session_id": token_user_id,
            "background_access_granted": True,
            "background_access_details": {
                "granted_at": refresh_token_data.get("created_at"),
                "scopes": refresh_token_data.get("scope", "").split(),
            },
        }

        if profile:
            response_data["idp_profile"] = profile

        return JSONResponse(response_data)

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "get_user_session")
        return JSONResponse(
            {"error": error_msg},
            status_code=500,
        )


async def revoke_user_access(request: Request) -> JSONResponse:
    """POST /api/v1/users/{user_id}/revoke - Revoke user's background access.

    Deletes the user's stored refresh token, removing their offline access.

    Requires OAuth bearer token. The user_id in the path must match
    the user_id in the token.
    """
    try:
        # Validate OAuth token and extract user
        token_user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/users/{{user_id}}/revoke: %s", e)
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "revoke_user_access"),
            },
            status_code=401,
        )

    # Get user_id from path
    path_user_id = request.path_params.get("user_id")

    # Verify token user matches requested user
    if token_user_id != path_user_id:
        logger.warning(
            "User %s attempted to revoke access for %s", token_user_id, path_user_id
        )
        return JSONResponse(
            {
                "error": "Forbidden",
                "message": "Cannot revoke another user's access",
            },
            status_code=403,
        )

    # Get token broker from app state
    oauth_context = request.app.state.oauth_context
    if oauth_context is None:
        logger.error("OAuth context not initialized")
        return JSONResponse(
            {"error": "OAuth not enabled"},
            status_code=500,
        )

    token_broker = oauth_context.get("token_broker")
    if not token_broker:
        logger.error("Token broker not available in app state")
        return JSONResponse(
            {"error": "Token broker not configured"},
            status_code=500,
        )

    try:
        # Delete refresh token from storage
        await token_broker.storage.delete_refresh_token(token_user_id)

        # CRITICAL: Invalidate all cached tokens for this user
        await token_broker.cache.invalidate(token_user_id)

        logger.info(
            "Revoked background access for user %s (cache and storage cleared)",
            token_user_id,
        )

        return JSONResponse(
            {
                "success": True,
                "message": f"Background access revoked for {token_user_id}",
            }
        )

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "revoke_user_access")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )
