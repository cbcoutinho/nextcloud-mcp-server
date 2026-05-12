"""Vector visualization routes for testing search algorithms.

Provides a web UI for users to test different search algorithms on their own
indexed documents and visualize results in 3D space using PCA.

All processing happens server-side following ADR-012:
- Search execution via shared search/algorithms.py
- Query embedding generation
- PCA dimensionality reduction (768-dim → 3D)
- Only 3D coordinates + metadata sent to client
- Bandwidth-efficient (3 floats per doc vs 768)
"""

import logging
import time
from pathlib import Path

import anyio
import numpy as np
from jinja2 import Environment, FileSystemLoader
from starlette.authentication import requires
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from nextcloud_mcp_server.api.management import _parse_int_param
from nextcloud_mcp_server.auth.userinfo_routes import (
    _get_authenticated_client_for_userinfo,
)
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding.service import get_embedding_service
from nextcloud_mcp_server.observability.tracing import trace_operation
from nextcloud_mcp_server.search import (
    BM25HybridSearchAlgorithm,
    SemanticSearchAlgorithm,
)
from nextcloud_mcp_server.search.context import (
    get_chunk_bbox_and_page_from_qdrant,
    get_chunk_with_context,
)
from nextcloud_mcp_server.utils.validation import is_valid_nextcloud_doc_id
from nextcloud_mcp_server.vector.oauth_sync import (
    NotProvisionedError,
    get_user_client_basic_auth,
)
from nextcloud_mcp_server.vector.pca import PCA
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)

# Setup Jinja2 environment for templates
_template_dir = Path(__file__).parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(_template_dir))


@requires("authenticated", redirect="oauth_login")
async def vector_visualization_html(request: Request) -> HTMLResponse:
    """Vector visualization page with search controls and interactive plot.

    Provides UI for testing search algorithms with real-time visualization.
    Requires vector sync to be enabled.

    Args:
        request: Starlette request object

    Returns:
        HTML page with search interface
    """
    settings = get_settings()

    if not settings.vector_sync_enabled:
        return HTMLResponse(
            """
            <div>
                <h2>Vector Visualization</h2>
                <div style="padding: 20px; background: #fff3cd; border: 1px solid #ffc107; border-radius: 4px;">
                    Vector sync is not enabled. Set VECTOR_SYNC_ENABLED=true to use this feature.
                </div>
            </div>
            """
        )

    # Get user info from auth context
    username = (
        request.user.display_name
        if hasattr(request.user, "display_name")
        else "unknown"
    )

    # Load and render template
    template = _jinja_env.get_template("vector_viz.html")
    html_content = template.render(username=username)
    return HTMLResponse(content=html_content)


@requires("authenticated", redirect="oauth_login")
async def vector_visualization_search(request: Request) -> JSONResponse:
    """Execute server-side search and return 3D coordinates + results.

    All processing happens server-side:
    1. Execute search via shared algorithm module
    2. Generate query embedding
    3. Fetch matching vectors from Qdrant
    4. Apply PCA reduction (768-dim → 3D) to query + documents
    5. Return coordinates + metadata only

    Args:
        request: Starlette request with query parameters

    Returns:
        JSON response with coordinates_3d and results (including query point)
    """
    settings = get_settings()

    if not settings.vector_sync_enabled:
        return JSONResponse(
            {"success": False, "error": "Vector sync not enabled"},
            status_code=400,
        )

    # Get user info from auth context
    username = (
        request.user.display_name if hasattr(request.user, "display_name") else None
    )

    if not username:
        return JSONResponse(
            {"success": False, "error": "User not authenticated"},
            status_code=401,
        )

    # Parse query parameters
    query = request.query_params.get("query", "")
    algorithm = request.query_params.get("algorithm", "bm25_hybrid")
    limit = int(request.query_params.get("limit", "50"))
    score_threshold = float(request.query_params.get("score_threshold", "0.0"))
    fusion = request.query_params.get("fusion", "rrf")  # Default to RRF

    # Parse doc_types (comma-separated list, None = all types)
    doc_types_param = request.query_params.get("doc_types", "")
    doc_types = doc_types_param.split(",") if doc_types_param else None

    logger.info(
        "Viz search: user=%s, query='%s', algorithm=%s, fusion=%s, limit=%s, doc_types=%s",
        username,
        query,
        algorithm,
        fusion,
        limit,
        doc_types,
    )

    try:
        # Start total request timer
        request_start = time.perf_counter()
        # Get authenticated HTTP client from session
        # In BasicAuth mode: uses username/password from session
        # In OAuth mode: uses access token from session
        with trace_operation("vector_viz.get_auth_client"):
            auth_client_ctx = await _get_authenticated_client_for_userinfo(request)

        async with auth_client_ctx as nc_client:  # noqa: F841
            # Create search algorithm (no client needed - verification removed)
            if algorithm == "semantic":
                search_algo = SemanticSearchAlgorithm(score_threshold=score_threshold)
            elif algorithm == "bm25_hybrid":
                search_algo = BM25HybridSearchAlgorithm(
                    score_threshold=score_threshold, fusion=fusion
                )
            else:
                return JSONResponse(
                    {"success": False, "error": f"Unknown algorithm: {algorithm}"},
                    status_code=400,
                )

            # Execute search (supports cross-app when doc_types=None)
            # Get unverified results with buffer for filtering
            search_start = time.perf_counter()
            all_results = []
            if doc_types is None or len(doc_types) == 0:
                # Cross-app search - search all indexed types
                with trace_operation(
                    "vector_viz.search_execute",
                    attributes={
                        "search.algorithm": algorithm,
                        "search.limit": limit * 2,
                        "search.doc_type": "all",
                    },
                ):
                    unverified_results = await search_algo.search(
                        query=query,
                        user_id=username,
                        limit=limit * 2,  # Buffer for verification filtering
                        doc_type=None,  # Search all types
                        score_threshold=score_threshold,
                    )
                all_results.extend(unverified_results)
            else:
                # Search each document type and combine
                for doc_type in doc_types:
                    with trace_operation(
                        "vector_viz.search_execute",
                        attributes={
                            "search.algorithm": algorithm,
                            "search.limit": limit * 2,
                            "search.doc_type": doc_type,
                        },
                    ):
                        unverified_results = await search_algo.search(
                            query=query,
                            user_id=username,
                            limit=limit * 2,  # Buffer for verification filtering
                            doc_type=doc_type,
                            score_threshold=score_threshold,
                        )
                    all_results.extend(unverified_results)
                # Sort by score before verification
                all_results.sort(key=lambda r: r.score, reverse=True)

            # No verification needed for visualization - we only need Qdrant metadata
            # (title, excerpt, doc_type) which is already in search results.
            # Verification is only needed for sampling (LLM needs full content).
            search_results = all_results[:limit]
            search_duration = time.perf_counter() - search_start

        # Store original scores and normalize for visualization
        # (best result = 1.0, worst result = 0.0 within THIS result set)
        # This makes visual encoding meaningful regardless of RRF normalization
        with trace_operation(
            "vector_viz.score_normalize",
            attributes={"normalize.num_results": len(search_results)},
        ):
            if search_results:
                scores = [r.score for r in search_results]
                min_score, max_score = min(scores), max(scores)
                score_range = max_score - min_score if max_score > min_score else 1.0

                logger.info(
                    "Normalizing scores for viz: original range [%s, %s] → [0.0, 1.0]",
                    format(min_score, ".3f"),
                    format(max_score, ".3f"),
                )

                # Store original score and rescale to 0-1 for visualization
                for r in search_results:
                    # Store original score before normalization
                    r.original_score = r.score
                    # Rescale for visual encoding
                    r.score = (r.score - min_score) / score_range

        if not search_results:
            return JSONResponse(
                {
                    "success": True,
                    "results": [],
                    "coordinates_3d": [],
                    "query_coords": [],
                    "message": "No results found",
                }
            )

        # Fetch vectors for specific matching chunks from Qdrant using batch retrieve
        vector_fetch_start = time.perf_counter()

        with trace_operation("vector_viz.get_qdrant_client"):
            qdrant_client = await get_qdrant_client()

        chunk_vectors_map = {}  # Map (doc_id, chunk_start, chunk_end) -> vector

        # Collect point IDs from search results for batch retrieval
        # point_id is the Qdrant internal ID returned by search algorithms
        point_ids = [r.point_id for r in search_results if r.point_id]

        if point_ids:
            # Single batch retrieve call instead of N sequential scroll calls
            # This is ~50x faster for 50 results (1 HTTP request vs 50)
            with trace_operation(
                "vector_viz.vector_retrieve",
                attributes={"retrieve.num_points": len(point_ids)},
            ):
                points_response = await qdrant_client.retrieve(
                    collection_name=settings.get_collection_name(),
                    ids=point_ids,
                    with_vectors=["dense"],
                    with_payload=["doc_id", "chunk_start_offset", "chunk_end_offset"],
                )

            # Build chunk_vectors_map from batch response
            for point in points_response:
                if point.vector is not None:
                    # Extract dense vector (handle both named and unnamed vectors)
                    if isinstance(point.vector, dict):
                        vector = point.vector.get("dense")
                    else:
                        vector = point.vector

                    if vector is not None and point.payload:
                        # SearchResult.id is str; coerce payload doc_id to match
                        # so the tuple lookup below succeeds even on legacy
                        # int-typed payloads written before normalization.
                        raw_doc_id = point.payload.get("doc_id")
                        doc_id = None if raw_doc_id is None else str(raw_doc_id)
                        chunk_start = point.payload.get("chunk_start_offset")
                        chunk_end = point.payload.get("chunk_end_offset")
                        chunk_key = (doc_id, chunk_start, chunk_end)
                        chunk_vectors_map[chunk_key] = vector

        vector_fetch_duration = time.perf_counter() - vector_fetch_start

        if len(chunk_vectors_map) < 2:
            # Not enough chunks for PCA
            return JSONResponse(
                {
                    "success": True,
                    "results": [
                        {
                            "id": r.id,
                            "doc_type": r.doc_type,
                            "title": r.title,
                            "excerpt": r.excerpt,
                            "score": r.score,
                            "metadata": r.metadata,
                        }
                        for r in search_results
                    ],
                    "coordinates_3d": [[0, 0, 0]] * len(search_results),
                    "query_coords": [0, 0, 0],
                    "message": "Not enough chunks for PCA",
                }
            )

        # Detect embedding dimension from first available vector
        embedding_dim = None
        for vector in chunk_vectors_map.values():
            if vector is not None:
                embedding_dim = len(vector)
                break

        if embedding_dim is None:
            return JSONResponse(
                {
                    "success": False,
                    "error": "Could not determine embedding dimension",
                },
                status_code=500,
            )

        logger.info("Detected embedding dimension: %s", embedding_dim)

        # Build chunk vectors array in search_results order (1:1 mapping)
        chunk_vectors = []
        for result in search_results:
            chunk_key = (result.id, result.chunk_start_offset, result.chunk_end_offset)
            if chunk_key in chunk_vectors_map:
                chunk_vectors.append(chunk_vectors_map[chunk_key])
            else:
                # Chunk not found in vectors (shouldn't happen)
                logger.warning(
                    "Chunk %s not found in fetched vectors, using zero vector",
                    chunk_key,
                )
                # Use zero vector as fallback
                chunk_vectors.append(np.zeros(embedding_dim))

        chunk_vectors = np.array(chunk_vectors)

        # Reuse query embedding from search algorithm (avoids redundant embedding call)
        query_embed_start = time.perf_counter()
        if search_algo.query_embedding is not None:
            query_embedding = search_algo.query_embedding
            logger.info(
                "Reusing query embedding from search algorithm (dimension=%s)",
                len(query_embedding),
            )
        else:
            # Fallback: generate embedding if not available from search
            embedding_service = get_embedding_service()
            query_embedding = await embedding_service.embed(query)
            logger.info(
                "Generated query embedding (dimension=%s)", len(query_embedding)
            )
        query_embed_duration = time.perf_counter() - query_embed_start

        # Combine query vector with chunk vectors for PCA
        # Query will be the last point in the array
        all_vectors = np.vstack([chunk_vectors, np.array([query_embedding])])

        # Normalize vectors to unit length (L2 normalization)
        # This is critical because Qdrant uses COSINE distance, which only measures
        # vector direction (angle), not magnitude. PCA uses Euclidean distance which
        # considers both direction and magnitude. By normalizing to unit length,
        # Euclidean distances in PCA space will match cosine distances.
        norms = np.linalg.norm(all_vectors, axis=1, keepdims=True)

        # Check for zero-norm vectors (can happen with empty/corrupted embeddings)
        zero_norm_mask = norms[:, 0] < 1e-10
        if zero_norm_mask.any():
            zero_indices = np.where(zero_norm_mask)[0]
            logger.warning(
                "Found %s zero-norm vectors at indices %s. Replacing with small epsilon to avoid division by zero.",
                zero_norm_mask.sum(),
                zero_indices.tolist(),
            )
            # Replace zero norms with small epsilon to avoid NaN
            norms[zero_norm_mask] = 1e-10

        all_vectors_normalized = all_vectors / norms
        logger.info(
            "Normalized vectors: query_norm=%s, doc_norm_range=[%s, %s]",
            format(norms[-1][0], ".3f"),
            format(norms[:-1].min(), ".3f"),
            format(norms[:-1].max(), ".3f"),
        )

        # Apply PCA dimensionality reduction (768-dim → 3D) on normalized vectors
        # Run in thread pool to avoid blocking the event loop (CPU-bound)
        pca_start = time.perf_counter()

        def _compute_pca(vectors: np.ndarray) -> tuple[np.ndarray, PCA]:
            pca = PCA(n_components=3)
            coords = pca.fit_transform(vectors)
            return coords, pca

        with trace_operation(
            "vector_viz.pca_compute",
            attributes={
                "pca.num_vectors": len(all_vectors_normalized),
                "pca.embedding_dim": embedding_dim,
            },
        ):
            coords_3d, pca = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                lambda: _compute_pca(all_vectors_normalized)
            )
        pca_duration = time.perf_counter() - pca_start

        # After fit, these attributes are guaranteed to be set
        assert pca.explained_variance_ratio_ is not None

        # Check for NaN values in PCA output (numerical instability)
        nan_mask = np.isnan(coords_3d)
        if nan_mask.any():
            nan_rows = np.where(nan_mask.any(axis=1))[0]
            logger.error(
                "Found NaN values in PCA output at %s points: %s. Replacing NaN with 0.0 to prevent JSON serialization error.",
                len(nan_rows),
                nan_rows.tolist()[:10],
            )
            # Replace NaN with 0 to allow JSON serialization
            coords_3d = np.nan_to_num(coords_3d, nan=0.0)

        # Split query coords from chunk coords
        # Round to 2 decimal places for cleaner display
        query_coords_3d = [
            round(float(x), 2) for x in coords_3d[-1]
        ]  # Last point is query
        chunk_coords_3d = coords_3d[:-1]  # All but last are chunks

        logger.info(
            "PCA explained variance: PC1=%s, PC2=%s, PC3=%s",
            format(pca.explained_variance_ratio_[0], ".3f"),
            format(pca.explained_variance_ratio_[1], ".3f"),
            format(pca.explained_variance_ratio_[2], ".3f"),
        )
        logger.info(
            "Embedding stats: chunks=%s, query_dim=%s, chunk_vector_dim=%s",
            len(chunk_vectors),
            len(query_embedding),
            chunk_vectors.shape[1] if chunk_vectors.size > 0 else 0,
        )

        # Coordinates already match search_results order (1:1 mapping)
        result_coords = [
            [round(float(x), 2) for x in coord] for coord in chunk_coords_3d
        ]

        # Build response
        response_results = [
            {
                "id": r.id,
                "doc_type": r.doc_type,
                "title": r.title,
                "excerpt": r.excerpt,
                "score": r.score,  # Normalized score for visual encoding (0-1)
                "original_score": getattr(
                    r, "original_score", r.score
                ),  # Raw score from algorithm
                "chunk_start_offset": r.chunk_start_offset,
                "chunk_end_offset": r.chunk_end_offset,
                "metadata": r.metadata,  # Include metadata (e.g., board_id for deck_card)
            }
            for r in search_results
        ]

        # Calculate total request duration
        total_duration = time.perf_counter() - request_start

        # Log comprehensive timing metrics
        logger.info(
            "Viz search timing: total=%sms, search=%sms (%s%%), vector_fetch=%sms (%s%%), query_embed=%sms (%s%%), pca=%sms (%s%%), results=%s, chunk_vectors=%s",
            format(total_duration * 1000, ".1f"),
            format(search_duration * 1000, ".1f"),
            format(search_duration / total_duration * 100, ".1f"),
            format(vector_fetch_duration * 1000, ".1f"),
            format(vector_fetch_duration / total_duration * 100, ".1f"),
            format(query_embed_duration * 1000, ".1f"),
            format(query_embed_duration / total_duration * 100, ".1f"),
            format(pca_duration * 1000, ".1f"),
            format(pca_duration / total_duration * 100, ".1f"),
            len(search_results),
            len(chunk_vectors),
        )

        return JSONResponse(
            {
                "success": True,
                "results": response_results,
                "coordinates_3d": result_coords[: len(search_results)],
                "query_coords": query_coords_3d,
                "pca_variance": {
                    "pc1": float(pca.explained_variance_ratio_[0]),
                    "pc2": float(pca.explained_variance_ratio_[1]),
                    "pc3": float(pca.explained_variance_ratio_[2]),
                },
                "timing": {
                    "total_ms": round(total_duration * 1000, 2),
                    "search_ms": round(search_duration * 1000, 2),
                    "vector_fetch_ms": round(vector_fetch_duration * 1000, 2),
                    "query_embed_ms": round(query_embed_duration * 1000, 2),
                    "pca_ms": round(pca_duration * 1000, 2),
                    "num_results": len(search_results),
                    "num_chunk_vectors": len(chunk_vectors),
                },
            }
        )

    except Exception as e:
        logger.error("Viz search error: %s", e, exc_info=True)
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )


@requires("authenticated", redirect="oauth_login")
async def chunk_context_endpoint(request: Request) -> JSONResponse:
    """Fetch chunk text with surrounding context for visualization.

    This endpoint retrieves the matched chunk along with surrounding text
    to provide context for the search result. Used by the viz pane to
    display chunks inline.

    Query parameters:
        doc_type: Document type (e.g., "note")
        doc_id: Document ID
        start: Chunk start offset (character position)
        end: Chunk end offset (character position)
        context: Characters of context before/after (default: 500)

    Returns:
        JSON with chunk_text, before_context, after_context, and flags
    """
    try:
        # Get query parameters
        doc_type = request.query_params.get("doc_type")
        doc_id = request.query_params.get("doc_id")
        start_str = request.query_params.get("start")
        end_str = request.query_params.get("end")
        chunk_index_str = request.query_params.get("chunk_index")
        total_chunks_str = request.query_params.get("total_chunks")

        # Validate required parameters
        if not all([doc_type, doc_id, start_str, end_str]):
            return JSONResponse(
                {
                    "success": False,
                    "error": "Missing required parameters: doc_type, doc_id, start, end",
                },
                status_code=400,
            )

        # Type assertions - we validated these above
        assert doc_type is not None
        assert doc_id is not None
        assert start_str is not None
        assert end_str is not None

        # Same numeric-doc_id gate as ``api/visualization.py`` — see the
        # canonical TODO and rationale there. Kept in sync so both
        # OAuth-protected and direct-access handlers reject malformed
        # IDs at the boundary instead of bottoming out as a 404 from
        # deep inside ``get_chunk_with_context``.
        if not is_valid_nextcloud_doc_id(doc_id):
            return JSONResponse(
                {
                    "success": False,
                    "error": f"doc_id must be numeric, got {doc_id!r}",
                },
                status_code=400,
            )

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
        chunk_index: int | None = None
        if chunk_index_str is not None:
            chunk_index = _parse_int_param(
                chunk_index_str, 0, 0, 1000000, "chunk_index"
            )
        total_chunks = _parse_int_param(total_chunks_str, 1, 1, 1000000, "total_chunks")
        # doc_id is keyword-indexed in Qdrant as str — pass through verbatim
        # (no int coercion; producers always stringify on write).

        user_id = request.user.display_name
        settings = get_settings()
        nextcloud_host = settings.nextcloud_host
        if not nextcloud_host:
            raise RuntimeError("Nextcloud host not configured")

        # Use the user's stored app password for Nextcloud calls.
        # The session cookie only authenticates the browser → MCP Server hop;
        # MCP Server → Nextcloud always uses the app password provisioned
        # during the authorization step.
        try:
            nc_client = await get_user_client_basic_auth(user_id, nextcloud_host)
        except NotProvisionedError as e:
            return JSONResponse(
                {"success": False, "error": str(e)},
                status_code=401,
            )

        async with nc_client:
            chunk_context = await get_chunk_with_context(
                nc_client=nc_client,
                user_id=user_id,
                doc_id=doc_id,
                doc_type=doc_type,
                chunk_start=start,
                chunk_end=end,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                context_chars=context_chars,
            )

        # Check if context expansion succeeded
        if chunk_context is None:
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Failed to fetch chunk context for {doc_type} {doc_id}",
                },
                status_code=404,
            )

        logger.info(
            "Fetched chunk context for %s_%s: chunk_len=%s, before_len=%s, after_len=%s",
            doc_type,
            doc_id,
            len(chunk_context.chunk_text),
            len(chunk_context.before_context),
            len(chunk_context.after_context),
        )

        # For PDF files, also fetch the chunk bbox from Qdrant so the client
        # can overlay a highlight on top of a render-on-demand page image
        # (Deck #76). Qdrant's page_number is trusted over the
        # context-expansion fallback when present.
        chunk_bbox = None
        page_number = chunk_context.page_number
        if doc_type == "file":
            qdrant_bbox, qdrant_page = await get_chunk_bbox_and_page_from_qdrant(
                user_id=user_id,
                doc_id=doc_id,
                chunk_index=chunk_index,
                chunk_start=start,
                chunk_end=end,
            )
            if qdrant_bbox is not None:
                chunk_bbox = qdrant_bbox
            if qdrant_page is not None:
                page_number = qdrant_page

        # Return response compatible with frontend expectations
        response_data: dict = {
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

        if chunk_bbox:
            response_data["chunk_bbox"] = chunk_bbox

        return JSONResponse(response_data)

    except ValueError as e:
        # User-supplied bad input → 400, not a server error.
        logger.warning("Invalid parameter format: %s", e)
        return JSONResponse(
            {"success": False, "error": f"Invalid parameter format: {e}"},
            status_code=400,
        )
    except Exception as e:
        logger.error("Chunk context error: %s", e, exc_info=True)
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )
