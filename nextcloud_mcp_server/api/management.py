"""Management API endpoints for Nextcloud PHP app integration.

ADR-018: Provides REST API endpoints for the Nextcloud PHP app to query:
- Server status and version
- User session information and background access status
- Vector sync metrics
- Vector search for visualization

All endpoints use OAuth bearer token authentication via UnifiedTokenVerifier.
The PHP app obtains tokens through PKCE flow and uses them to access these endpoints.
"""

import logging
import os
import time
from importlib.metadata import version
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


# Get package version from metadata
__version__ = version("nextcloud-mcp-server")

# Track server start time for uptime calculation
_server_start_time = time.time()


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

    # Validate token (handles both JWT and opaque tokens)
    # verify_token returns AccessToken object or None
    access_token = await token_verifier.verify_token(token)

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
    logger.error(f"Error in {context}: {error}", exc_info=True)

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
    from nextcloud_mcp_server.config import get_settings

    settings = get_settings()

    # Calculate uptime
    uptime_seconds = int(time.time() - _server_start_time)

    # Determine auth mode using proper mode detection
    from nextcloud_mcp_server.config_validators import AuthMode, detect_auth_mode

    mode = detect_auth_mode(settings)

    # Map deployment mode to auth_mode for API response
    # This helps clients (like Astrolabe) determine which auth flow to use
    if mode == AuthMode.OAUTH_SINGLE_AUDIENCE or mode == AuthMode.OAUTH_TOKEN_EXCHANGE:
        auth_mode = "oauth"
    elif mode == AuthMode.MULTI_USER_BASIC:
        auth_mode = "multi_user_basic"
    elif mode == AuthMode.SINGLE_USER_BASIC:
        auth_mode = "basic"
    elif mode == AuthMode.SMITHERY_STATELESS:
        auth_mode = "smithery"
    else:
        auth_mode = "unknown"

    response_data = {
        "version": __version__,
        "auth_mode": auth_mode,
        "vector_sync_enabled": settings.vector_sync_enabled,
        "uptime_seconds": uptime_seconds,
        "management_api_version": "1.0",
    }

    # Add app password support indicator for multi-user BasicAuth mode
    if mode == AuthMode.MULTI_USER_BASIC:
        response_data["supports_app_passwords"] = settings.enable_offline_access

    # Include OIDC configuration if in OAuth mode
    if auth_mode == "oauth":
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

    from nextcloud_mcp_server.config import get_settings

    settings = get_settings()
    if not settings.vector_sync_enabled:
        return JSONResponse(
            {"error": "Vector sync is disabled on this server"},
            status_code=404,
        )

    try:
        # Get document receive stream from app state (set by starlette_lifespan in app.py)
        document_receive_stream = getattr(
            request.app.state, "document_receive_stream", None
        )

        if document_receive_stream is None:
            logger.debug("document_receive_stream not available in app state")
            return JSONResponse(
                {
                    "status": "unknown",
                    "indexed_documents": 0,
                    "pending_documents": 0,
                    "message": "Vector sync stream not initialized",
                }
            )

        # Get pending count from stream statistics
        stream_stats = document_receive_stream.statistics()
        pending_count = stream_stats.current_buffer_used

        # Get Qdrant client and query indexed count
        indexed_count = 0
        try:
            from qdrant_client.models import Filter

            from nextcloud_mcp_server.vector.placeholder import get_placeholder_filter
            from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

            qdrant_client = await get_qdrant_client()

            # Count documents in collection, excluding placeholders
            count_result = await qdrant_client.count(
                collection_name=settings.get_collection_name(),
                count_filter=Filter(must=[get_placeholder_filter()]),
            )
            indexed_count = count_result.count

        except Exception as e:
            logger.warning(f"Failed to query Qdrant for indexed count: {e}")
            # Continue with indexed_count = 0

        # Determine status
        status = "syncing" if pending_count > 0 else "idle"

        return JSONResponse(
            {
                "status": status,
                "indexed_documents": indexed_count,
                "pending_documents": pending_count,
            }
        )

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
            f"User {token_user_id} attempted to access session for {path_user_id}"
        )
        return JSONResponse(
            {
                "error": "Forbidden",
                "message": "Cannot access another user's session",
            },
            status_code=403,
        )

    # Check if offline access is enabled
    enable_offline_access = os.getenv("ENABLE_OFFLINE_ACCESS", "false").lower() in (
        "true",
        "1",
        "yes",
    )

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
        logger.warning(f"Unauthorized access to /api/v1/users/{{user_id}}/revoke: {e}")
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
            f"User {token_user_id} attempted to revoke access for {path_user_id}"
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
            f"Revoked background access for user {token_user_id} (cache and storage cleared)"
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


async def get_installed_apps(request: Request) -> JSONResponse:
    """GET /api/v1/apps - Get list of installed Nextcloud apps.

    Returns a list of installed app IDs for filtering webhook presets.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning(f"Unauthorized access to /api/v1/apps: {e}")
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "get_installed_apps"),
            },
            status_code=401,
        )

    try:
        import httpx

        # Get Bearer token from request
        token = extract_bearer_token(request)
        if not token:
            raise ValueError("Missing Authorization header")

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Create authenticated HTTP client
        async with httpx.AsyncClient(
            base_url=nextcloud_host,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            # Get installed apps using OCS API
            # Notes, Calendar, Deck, Tables, etc. are apps that support webhooks
            # We check which ones are installed and enabled
            ocs_url = "/ocs/v1.php/cloud/apps"
            params = {"filter": "enabled"}

            response = await client.get(
                ocs_url,
                params=params,
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            )

            if response.status_code != 200:
                raise ValueError(f"OCS API returned status {response.status_code}")

            data = response.json()
            apps = data.get("ocs", {}).get("data", {}).get("apps", [])

            return JSONResponse({"apps": apps})

    except Exception as e:
        logger.error(f"Error getting installed apps for user {user_id}: {e}")
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "get_installed_apps"),
            },
            status_code=500,
        )


async def list_webhooks(request: Request) -> JSONResponse:
    """GET /api/v1/webhooks - List all registered webhooks.

    Returns list of webhook registrations for the authenticated user.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning(f"Unauthorized access to /api/v1/webhooks: {e}")
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "list_webhooks"),
            },
            status_code=401,
        )

    try:
        import httpx

        from nextcloud_mcp_server.client.webhooks import WebhooksClient

        # Get Bearer token from request
        token = extract_bearer_token(request)
        if not token:
            raise ValueError("Missing Authorization header")

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Create authenticated HTTP client
        async with httpx.AsyncClient(
            base_url=nextcloud_host,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            # Use WebhooksClient to list webhooks
            webhooks_client = WebhooksClient(client, user_id)
            webhooks = await webhooks_client.list_webhooks()

            return JSONResponse({"webhooks": webhooks})

    except Exception as e:
        logger.error(f"Error listing webhooks for user {user_id}: {e}")
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "list_webhooks"),
            },
            status_code=500,
        )


async def create_webhook(request: Request) -> JSONResponse:
    """POST /api/v1/webhooks - Create a new webhook registration.

    Request body:
    {
        "event": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
        "uri": "http://mcp:8000/webhooks/nextcloud",
        "eventFilter": {"event.node.path": "/^\\/.*\\/files\\/Notes\\//"}
    }

    Returns the created webhook data including the webhook ID.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning(f"Unauthorized access to /api/v1/webhooks: {e}")
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "create_webhook"),
            },
            status_code=401,
        )

    try:
        import httpx

        from nextcloud_mcp_server.client.webhooks import WebhooksClient

        # Parse request body
        body = await request.json()
        event = body.get("event")
        uri = body.get("uri")
        # Accept both camelCase (eventFilter) and snake_case (event_filter)
        event_filter = body.get("eventFilter") or body.get("event_filter")

        if not event or not uri:
            return JSONResponse(
                {
                    "error": "Bad request",
                    "message": "Missing required fields: event, uri",
                },
                status_code=400,
            )

        # Get Bearer token from request
        token = extract_bearer_token(request)
        if not token:
            raise ValueError("Missing Authorization header")

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Create authenticated HTTP client
        async with httpx.AsyncClient(
            base_url=nextcloud_host,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            # Use WebhooksClient to create webhook
            webhooks_client = WebhooksClient(client, user_id)
            webhook_data = await webhooks_client.create_webhook(
                event=event, uri=uri, event_filter=event_filter
            )

            return JSONResponse({"webhook": webhook_data})

    except Exception as e:
        logger.error(f"Error creating webhook for user {user_id}: {e}")
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "create_webhook"),
            },
            status_code=500,
        )


async def delete_webhook(request: Request) -> JSONResponse:
    """DELETE /api/v1/webhooks/{webhook_id} - Delete a webhook registration.

    Returns success/failure status.

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning(f"Unauthorized access to /api/v1/webhooks: {e}")
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "delete_webhook"),
            },
            status_code=401,
        )

    try:
        import httpx

        from nextcloud_mcp_server.client.webhooks import WebhooksClient

        # Get webhook_id from path parameter
        webhook_id = request.path_params.get("webhook_id")
        if not webhook_id:
            return JSONResponse(
                {"error": "Bad request", "message": "Missing webhook_id"},
                status_code=400,
            )

        try:
            webhook_id = int(webhook_id)
        except ValueError:
            return JSONResponse(
                {"error": "Bad request", "message": "Invalid webhook_id"},
                status_code=400,
            )

        # Get Bearer token from request
        token = extract_bearer_token(request)
        if not token:
            raise ValueError("Missing Authorization header")

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Create authenticated HTTP client
        async with httpx.AsyncClient(
            base_url=nextcloud_host,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            # Use WebhooksClient to delete webhook
            webhooks_client = WebhooksClient(client, user_id)
            await webhooks_client.delete_webhook(webhook_id=webhook_id)

            return JSONResponse({"success": True, "message": "Webhook deleted"})

    except Exception as e:
        logger.error(f"Error deleting webhook for user {user_id}: {e}")
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "delete_webhook"),
            },
            status_code=500,
        )


async def unified_search(request: Request) -> JSONResponse:
    """POST /api/v1/search - Search endpoint for Nextcloud Unified Search.

    Optimized search endpoint for the Nextcloud Unified Search provider
    and other PHP app integrations. Returns results with metadata needed
    for navigation to source documents.

    Request body:
    {
        "query": "search query",
        "algorithm": "semantic|bm25|hybrid",  // default: hybrid
        "limit": 20,  // max: 100
        "offset": 0,  // pagination offset
        "include_pca": false,  // optional PCA coordinates
        "include_chunks": true  // include text snippets
    }

    Response:
    {
        "results": [{
            "id": "doc123",
            "doc_type": "note",
            "title": "Document Title",
            "excerpt": "Matching text snippet...",
            "score": 0.85,
            "path": "/path/to/file.txt",  // for files
            "board_id": 1,  // for deck cards
            "card_id": 42
        }],
        "total_found": 150,
        "algorithm_used": "hybrid"
    }

    Requires OAuth bearer token for user filtering.
    """
    from nextcloud_mcp_server.config import get_settings

    settings = get_settings()
    if not settings.vector_sync_enabled:
        return JSONResponse(
            {"error": "Vector sync is disabled on this server"},
            status_code=404,
        )

    # Validate OAuth token and extract user
    try:
        user_id, _validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning(f"Unauthorized access to /api/v1/search: {e}")
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "unified_search"),
            },
            status_code=401,
        )

    try:
        # Parse request body
        body = await request.json()

        # Validate and parse parameters
        try:
            query = body.get("query", "")
            _validate_query_string(query, max_length=10000)

            limit = _parse_int_param(
                str(body.get("limit")) if body.get("limit") is not None else None,
                20,
                1,
                100,
                "limit",
            )

            offset = _parse_int_param(
                str(body.get("offset")) if body.get("offset") is not None else None,
                0,
                0,
                1000000,
                "offset",
            )

            score_threshold = _parse_float_param(
                body.get("score_threshold"),
                0.0,
                0.0,
                1.0,
                "score_threshold",
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        algorithm = body.get("algorithm", "hybrid")
        fusion = body.get("fusion", "rrf")
        include_pca = body.get("include_pca", False)
        include_chunks = body.get("include_chunks", True)
        doc_types = body.get("doc_types")  # Optional filter

        if not query:
            return JSONResponse({"results": [], "total_found": 0})

        # Validate algorithm
        valid_algorithms = {"semantic", "bm25", "hybrid"}
        if algorithm not in valid_algorithms:
            algorithm = "hybrid"

        # Validate fusion method
        valid_fusions = {"rrf", "dbsf"}
        if fusion not in valid_fusions:
            fusion = "rrf"

        # Execute search using the appropriate algorithm
        from nextcloud_mcp_server.search import (
            BM25HybridSearchAlgorithm,
            SemanticSearchAlgorithm,
        )

        # Select search algorithm
        if algorithm == "semantic":
            search_algo = SemanticSearchAlgorithm(score_threshold=score_threshold)
        else:
            search_algo = BM25HybridSearchAlgorithm(
                score_threshold=score_threshold, fusion=fusion
            )

        # Request extra results to handle offset
        search_limit = limit + offset

        # Execute search
        all_results = []
        if doc_types and isinstance(doc_types, list):
            for doc_type in doc_types:
                if doc_type:
                    results = await search_algo.search(
                        query=query,
                        user_id=user_id,
                        limit=search_limit,
                        doc_type=doc_type,
                    )
                    all_results.extend(results)
            all_results.sort(key=lambda r: r.score, reverse=True)
        else:
            all_results = await search_algo.search(
                query=query,
                user_id=user_id,
                limit=search_limit,
            )

        # Sort results by score (no deduplication - show all chunks)
        sorted_results = sorted(all_results, key=lambda r: r.score, reverse=True)

        # Calculate total and apply pagination
        total_found = len(sorted_results)
        paginated_results = sorted_results[offset : offset + limit]

        # Format results for Unified Search
        formatted_results = []
        for result in paginated_results:
            # Get document ID (prefer note_id for notes)
            doc_id = result.id
            if result.metadata and "note_id" in result.metadata:
                doc_id = result.metadata["note_id"]

            result_data: dict[str, Any] = {
                "id": doc_id,
                "doc_type": result.doc_type,
                "title": result.title,
                "score": result.score,
            }

            # Include excerpt/chunk if requested (full content, no truncation)
            if include_chunks and result.excerpt:
                result_data["excerpt"] = result.excerpt

            # Include navigation metadata from result.metadata
            if result.metadata:
                # File path and mimetype for files
                if "path" in result.metadata:
                    result_data["path"] = result.metadata["path"]
                if "mime_type" in result.metadata:
                    result_data["mime_type"] = result.metadata["mime_type"]

                # Deck card navigation
                if "board_id" in result.metadata:
                    result_data["board_id"] = result.metadata["board_id"]
                if "card_id" in result.metadata:
                    result_data["card_id"] = result.metadata["card_id"]

                # Calendar event metadata
                if "calendar_id" in result.metadata:
                    result_data["calendar_id"] = result.metadata["calendar_id"]
                if "event_uid" in result.metadata:
                    result_data["event_uid"] = result.metadata["event_uid"]

            # Add PDF page metadata
            if result.page_number is not None:
                result_data["page_number"] = result.page_number
            if result.page_count is not None:
                result_data["page_count"] = result.page_count

            # Add chunk metadata (always present, defaults to 0 and 1)
            result_data["chunk_index"] = result.chunk_index
            result_data["total_chunks"] = result.total_chunks

            # Add chunk offsets for modal navigation
            if result.chunk_start_offset is not None:
                result_data["chunk_start_offset"] = result.chunk_start_offset
            if result.chunk_end_offset is not None:
                result_data["chunk_end_offset"] = result.chunk_end_offset

            formatted_results.append(result_data)

        response_data: dict[str, Any] = {
            "results": formatted_results,
            "total_found": total_found,
            "algorithm_used": algorithm,
        }

        # Optional PCA coordinates
        if include_pca and len(paginated_results) >= 2:
            try:
                from nextcloud_mcp_server.vector.visualization import (
                    compute_pca_coordinates,
                )

                if search_algo.query_embedding is not None:
                    query_embedding = search_algo.query_embedding
                else:
                    from nextcloud_mcp_server.embedding.service import (
                        get_embedding_service,
                    )

                    embedding_service = get_embedding_service()
                    query_embedding = await embedding_service.embed(query)

                pca_data = await compute_pca_coordinates(
                    paginated_results, query_embedding
                )
                response_data["pca_data"] = pca_data
            except Exception as e:
                logger.warning(f"Failed to compute PCA for unified search: {e}")

        return JSONResponse(response_data)

    except Exception as e:
        logger.error(f"Error in unified search: {e}")
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "unified_search"),
            },
            status_code=500,
        )


async def vector_search(request: Request) -> JSONResponse:
    """POST /api/v1/vector-viz/search - Vector search for visualization.

    Executes semantic search and returns results with optional PCA coordinates
    for 2D visualization.

    Request body:
    {
        "query": "search query",
        "algorithm": "semantic|bm25|hybrid",  // default: hybrid
        "limit": 10,  // max: 50
        "include_pca": true,  // whether to include 2D coordinates
        "doc_types": ["note", "file"]  // optional filter by document types
    }

    Requires OAuth bearer token for user filtering.
    """
    from nextcloud_mcp_server.config import get_settings

    settings = get_settings()
    if not settings.vector_sync_enabled:
        return JSONResponse(
            {"error": "Vector sync is disabled on this server"},
            status_code=404,
        )

    # Validate OAuth token and extract user
    try:
        user_id, _validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning(f"Unauthorized access to /api/v1/vector-viz/search: {e}")
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "vector_search"),
            },
            status_code=401,
        )

    try:
        # Parse request body
        body = await request.json()
        query = body.get("query", "")
        algorithm = body.get("algorithm", "hybrid")
        fusion = body.get("fusion", "rrf")
        score_threshold = body.get("score_threshold", 0.0)
        limit = min(body.get("limit", 10), 50)  # Enforce max limit
        include_pca = body.get("include_pca", True)
        doc_types = body.get("doc_types")  # Optional list of document types

        if not query:
            return JSONResponse(
                {"error": "Missing required parameter: query"},
                status_code=400,
            )

        # Validate algorithm
        valid_algorithms = {"semantic", "bm25", "hybrid"}
        if algorithm not in valid_algorithms:
            algorithm = "hybrid"

        # Validate fusion method
        valid_fusions = {"rrf", "dbsf"}
        if fusion not in valid_fusions:
            fusion = "rrf"

        # Execute search using the appropriate algorithm
        from nextcloud_mcp_server.search import (
            BM25HybridSearchAlgorithm,
            SemanticSearchAlgorithm,
        )

        # Select search algorithm
        if algorithm == "semantic":
            search_algo = SemanticSearchAlgorithm(score_threshold=score_threshold)
        else:
            # Both "hybrid" and "bm25" use the BM25HybridSearchAlgorithm
            # which combines dense semantic and sparse BM25 vectors
            search_algo = BM25HybridSearchAlgorithm(
                score_threshold=score_threshold, fusion=fusion
            )

        # Execute search for each doc_type if specified, otherwise search all
        all_results = []
        if doc_types and isinstance(doc_types, list):
            # Search each doc_type separately and merge results
            for doc_type in doc_types:
                if doc_type:  # Skip empty strings
                    results = await search_algo.search(
                        query=query,
                        user_id=user_id,
                        limit=limit,
                        doc_type=doc_type,
                    )
                    all_results.extend(results)
            # Sort merged results by score and limit
            all_results.sort(key=lambda r: r.score, reverse=True)
            all_results = all_results[:limit]
        else:
            # Search all document types
            all_results = await search_algo.search(
                query=query,
                user_id=user_id,
                limit=limit,
            )

        # Format results for PHP client
        formatted_results = []
        for result in all_results:
            formatted_result = {
                "id": result.id,
                "doc_type": result.doc_type,
                "title": result.title,
                "excerpt": result.excerpt[:200] if result.excerpt else "",
                "score": result.score,
                "metadata": result.metadata,
                # Chunk information for context display
                "chunk_index": result.chunk_index,
                "total_chunks": result.total_chunks,
            }
            # Include optional fields if present
            if result.chunk_start_offset is not None:
                formatted_result["chunk_start_offset"] = result.chunk_start_offset
            if result.chunk_end_offset is not None:
                formatted_result["chunk_end_offset"] = result.chunk_end_offset
            if result.page_number is not None:
                formatted_result["page_number"] = result.page_number
            if result.page_count is not None:
                formatted_result["page_count"] = result.page_count
            formatted_results.append(formatted_result)

        response_data: dict[str, Any] = {
            "results": formatted_results,
            "algorithm_used": algorithm,
            "total_documents": len(formatted_results),
        }

        # Compute PCA coordinates for visualization using shared function
        if include_pca and len(all_results) >= 2:
            try:
                from nextcloud_mcp_server.vector.visualization import (
                    compute_pca_coordinates,
                )

                # Get query embedding from search algorithm or generate it
                if search_algo.query_embedding is not None:
                    query_embedding = search_algo.query_embedding
                else:
                    from nextcloud_mcp_server.embedding.service import (
                        get_embedding_service,
                    )

                    embedding_service = get_embedding_service()
                    query_embedding = await embedding_service.embed(query)

                pca_data = await compute_pca_coordinates(all_results, query_embedding)
                response_data["coordinates_3d"] = pca_data["coordinates_3d"]
                response_data["query_coords"] = pca_data["query_coords"]
                if "pca_variance" in pca_data:
                    response_data["pca_variance"] = pca_data["pca_variance"]
            except Exception as e:
                logger.warning(f"Failed to compute PCA coordinates: {e}")
                response_data["coordinates_3d"] = []
                response_data["query_coords"] = []
        elif include_pca:
            # Not enough results for PCA
            response_data["coordinates_3d"] = []
            response_data["query_coords"] = []

        return JSONResponse(response_data)

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "vector_search")
        return JSONResponse(
            {"error": error_msg},
            status_code=500,
        )


async def get_chunk_context(request: Request) -> JSONResponse:
    """GET /api/v1/chunk-context - Fetch chunk text with context.

    Retrieves the matched chunk along with surrounding text and metadata.
    Used by clients to display chunk context and highlighted PDFs.

    Query parameters:
        doc_type: Document type (e.g., "note")
        doc_id: Document ID
        start: Chunk start offset (character position)
        end: Chunk end offset (character position)
        context: Characters of context before/after (default: 500)

    Requires OAuth bearer token for authentication.
    """
    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning(f"Unauthorized access to /api/v1/chunk-context: {e}")
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "get_chunk_context"),
            },
            status_code=401,
        )

    try:
        # Get query parameters
        doc_type = request.query_params.get("doc_type")
        doc_id = request.query_params.get("doc_id")
        start_str = request.query_params.get("start")
        end_str = request.query_params.get("end")

        # Validate required parameters
        if not all([doc_type, doc_id, start_str, end_str]):
            return JSONResponse(
                {
                    "success": False,
                    "error": "Missing required parameters: doc_type, doc_id, start, end",
                },
                status_code=400,
            )

        # Type narrowing: we already checked these are not None above
        assert start_str is not None
        assert end_str is not None
        assert doc_id is not None
        assert doc_type is not None

        # Parse and validate integer parameters with bounds checking
        try:
            context_chars = _parse_int_param(
                request.query_params.get("context"),
                500,
                0,
                10000,
                "context_chars",
            )
            start = _parse_int_param(start_str, 0, 0, 10000000, "start")
            end = _parse_int_param(end_str, 0, 0, 10000000, "end")
            if end <= start:
                raise ValueError("end must be greater than start")
        except ValueError as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=400)
        # Convert doc_id to int if possible (most IDs are int)
        doc_id_val: str | int = int(doc_id) if doc_id.isdigit() else doc_id

        # Get bearer token for client initialization
        token = extract_bearer_token(request)
        if not token:
            raise ValueError("Missing token")

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Initialize authenticated Nextcloud client
        from nextcloud_mcp_server.client import NextcloudClient
        from nextcloud_mcp_server.search.context import get_chunk_with_context

        async with NextcloudClient.from_token(
            base_url=nextcloud_host, token=token, username=user_id
        ) as nc_client:
            chunk_context = await get_chunk_with_context(
                nc_client=nc_client,
                user_id=user_id,
                doc_id=doc_id_val,
                doc_type=doc_type,
                chunk_start=start,
                chunk_end=end,
                context_chars=context_chars,
            )

        if chunk_context is None:
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Failed to fetch chunk context for {doc_type} {doc_id}",
                },
                status_code=404,
            )

        # For PDF files, also fetch the highlighted page image from Qdrant if available
        # This is useful for clients that want to show a pre-rendered image
        highlighted_page_image = None
        page_number = chunk_context.page_number

        if doc_type == "file":
            try:
                from qdrant_client.models import FieldCondition, Filter, MatchValue

                from nextcloud_mcp_server.config import get_settings
                from nextcloud_mcp_server.vector.placeholder import (
                    get_placeholder_filter,
                )
                from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

                settings = get_settings()
                qdrant_client = await get_qdrant_client()

                # Query for this specific chunk's highlighted image
                points_response = await qdrant_client.scroll(
                    collection_name=settings.get_collection_name(),
                    scroll_filter=Filter(
                        must=[
                            get_placeholder_filter(),
                            FieldCondition(
                                key="doc_id", match=MatchValue(value=doc_id_val)
                            ),
                            FieldCondition(
                                key="user_id", match=MatchValue(value=user_id)
                            ),
                            FieldCondition(
                                key="chunk_start_offset", match=MatchValue(value=start)
                            ),
                            FieldCondition(
                                key="chunk_end_offset", match=MatchValue(value=end)
                            ),
                        ]
                    ),
                    limit=1,
                    with_vectors=False,
                    with_payload=["highlighted_page_image", "page_number"],
                )

                if points_response[0]:
                    payload = points_response[0][0].payload
                    if payload:
                        highlighted_page_image = payload.get("highlighted_page_image")
                        # Trust Qdrant page number if available (might be more accurate than context expansion logic)
                        if payload.get("page_number") is not None:
                            page_number = payload.get("page_number")

            except Exception as e:
                logger.warning(f"Failed to fetch highlighted image: {e}")

        # Build response
        response_data = {
            "success": True,
            "chunk_text": chunk_context.chunk_text,
            "before_context": chunk_context.before_context,
            "after_context": chunk_context.after_context,
            "has_more_before": chunk_context.has_before_truncation,
            "has_more_after": chunk_context.has_after_truncation,
            "page_number": page_number,
            "chunk_index": chunk_context.chunk_index,
            "total_chunks": chunk_context.total_chunks,
        }

        if highlighted_page_image:
            response_data["highlighted_page_image"] = highlighted_page_image

        return JSONResponse(response_data)

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "get_chunk_context")
        return JSONResponse(
            {"error": error_msg},
            status_code=500,
        )
