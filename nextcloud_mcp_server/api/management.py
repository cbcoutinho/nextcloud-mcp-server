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

    # Determine auth mode
    nextcloud_username = os.getenv("NEXTCLOUD_USERNAME")
    nextcloud_password = os.getenv("NEXTCLOUD_PASSWORD")

    if nextcloud_username and nextcloud_password:
        auth_mode = "basic"
    else:
        auth_mode = "oauth"

    response_data = {
        "version": __version__,
        "auth_mode": auth_mode,
        "vector_sync_enabled": settings.vector_sync_enabled,
        "uptime_seconds": uptime_seconds,
        "management_api_version": "1.0",
    }

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
        logger.error(f"Error getting vector sync status: {e}")
        return JSONResponse(
            {"error": "Internal error", "message": str(e)},
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
        logger.warning(f"Unauthorized access to /api/v1/users/{{user_id}}/session: {e}")
        return JSONResponse(
            {"error": "Unauthorized", "message": str(e)},
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
        logger.error(f"Error getting user session for {token_user_id}: {e}")
        return JSONResponse(
            {"error": "Internal error", "message": str(e)},
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
            {"error": "Unauthorized", "message": str(e)},
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

    # Get refresh token storage from app state
    storage = request.app.state.oauth_context.get("storage")
    if not storage:
        logger.error("Refresh token storage not available in app state")
        return JSONResponse(
            {"error": "Storage not configured"},
            status_code=500,
        )

    try:
        # Delete refresh token
        await storage.delete_refresh_token(token_user_id)
        logger.info(f"Revoked background access for user: {token_user_id}")

        return JSONResponse(
            {
                "success": True,
                "message": f"Background access revoked for {token_user_id}",
            }
        )

    except Exception as e:
        logger.error(f"Error revoking access for {token_user_id}: {e}")
        return JSONResponse(
            {"error": "Internal error", "message": str(e)},
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
            {"error": "Unauthorized", "message": str(e)},
            status_code=401,
        )

    try:
        # Parse request body
        body = await request.json()
        query = body.get("query", "")
        algorithm = body.get("algorithm", "hybrid")
        fusion = body.get("fusion", "rrf")
        score_threshold = body.get("score_threshold", 0.0)
        limit = min(body.get("limit", 20), 100)  # Enforce max limit
        offset = body.get("offset", 0)
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

        # Validate score threshold
        score_threshold = max(0.0, min(1.0, float(score_threshold)))

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

        # Deduplicate results by document (multiple chunks may come from same doc)
        # Keep highest-scoring chunk per document
        doc_map: dict[str, Any] = {}  # key: "doc_type:id" -> best result
        for result in all_results:
            # Build document key from type and ID
            doc_id = result.id
            if result.metadata:
                # Use note_id if present (for notes), otherwise use result.id
                doc_id = result.metadata.get("note_id", result.id)
            doc_key = f"{result.doc_type}:{doc_id}"

            # Keep only the highest-scoring chunk per document
            if doc_key not in doc_map or result.score > doc_map[doc_key].score:
                doc_map[doc_key] = result

        # Convert back to list and sort by score
        deduplicated_results = sorted(
            doc_map.values(), key=lambda r: r.score, reverse=True
        )

        # Calculate total and apply pagination (on deduplicated results)
        total_found = len(deduplicated_results)
        paginated_results = deduplicated_results[offset : offset + limit]

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
            {"error": "Internal error", "message": str(e)},
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
            {"error": "Unauthorized", "message": str(e)},
            status_code=401,
        )

    try:
        # Parse request body
        body = await request.json()
        query = body.get("query", "")
        algorithm = body.get("algorithm", "hybrid")
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

        # Execute search using the appropriate algorithm
        from nextcloud_mcp_server.search import (
            BM25HybridSearchAlgorithm,
            SemanticSearchAlgorithm,
        )

        # Select search algorithm
        if algorithm == "semantic":
            search_algo = SemanticSearchAlgorithm(score_threshold=0.0)
        else:
            # Both "hybrid" and "bm25" use the BM25HybridSearchAlgorithm
            # which combines dense semantic and sparse BM25 vectors
            search_algo = BM25HybridSearchAlgorithm(score_threshold=0.0, fusion="rrf")

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
            formatted_results.append(
                {
                    "id": result.id,
                    "doc_type": result.doc_type,
                    "title": result.title,
                    "excerpt": result.excerpt[:200] if result.excerpt else "",
                    "score": result.score,
                    "metadata": result.metadata,
                }
            )

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
        logger.error(f"Error executing vector search: {e}")
        return JSONResponse(
            {"error": "Internal error", "message": str(e)},
            status_code=500,
        )
