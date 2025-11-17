"""Vector visualization routes for testing search algorithms.

Provides a web UI for users to test different search algorithms on their own
indexed documents and visualize results in 2D space using PCA.

All processing happens server-side following ADR-012:
- Search execution via shared search/algorithms.py
- PCA dimensionality reduction (768-dim → 2D)
- Only 2D coordinates + metadata sent to client
- Bandwidth-efficient (2 floats per doc vs 768)
"""

import logging
import time
from pathlib import Path

import numpy as np
from jinja2 import Environment, FileSystemLoader
from starlette.authentication import requires
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.search import (
    BM25HybridSearchAlgorithm,
    SemanticSearchAlgorithm,
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
    """Execute server-side search and return 2D coordinates + results.

    All processing happens server-side:
    1. Execute search via shared algorithm module
    2. Fetch matching vectors from Qdrant
    3. Apply PCA reduction (768-dim → 2D)
    4. Return coordinates + metadata only

    Args:
        request: Starlette request with query parameters

    Returns:
        JSON response with coordinates_2d and results
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
        f"Viz search: user={username}, query='{query}', "
        f"algorithm={algorithm}, fusion={fusion}, limit={limit}, doc_types={doc_types}"
    )

    try:
        # Start total request timer
        request_start = time.perf_counter()
        # Get authenticated HTTP client from session
        # In BasicAuth mode: uses username/password from session
        # In OAuth mode: uses access token from session
        from nextcloud_mcp_server.auth.userinfo_routes import (
            _get_authenticated_client_for_userinfo,
        )

        async with await _get_authenticated_client_for_userinfo(request) as http_client:  # noqa: F841
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
        if search_results:
            scores = [r.score for r in search_results]
            min_score, max_score = min(scores), max(scores)
            score_range = max_score - min_score if max_score > min_score else 1.0

            logger.info(
                f"Normalizing scores for viz: original range [{min_score:.3f}, {max_score:.3f}] "
                f"→ [0.0, 1.0]"
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
                    "coordinates_2d": [],
                    "message": "No results found",
                }
            )

        # Fetch vectors for matching results from Qdrant
        vector_fetch_start = time.perf_counter()
        qdrant_client = await get_qdrant_client()
        doc_ids = [r.id for r in search_results]

        # Retrieve vectors for the matching documents
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        points_response = await qdrant_client.scroll(
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="doc_id",
                        match=MatchAny(any=[str(doc_id) for doc_id in doc_ids]),
                    ),
                    FieldCondition(
                        key="user_id",
                        match={"value": username},
                    ),
                ]
            ),
            limit=len(doc_ids) * 2,  # Account for multiple chunks per doc
            with_vectors=["dense"],  # Only fetch dense vectors for visualization
            with_payload=["doc_id"],  # Need doc_id to map vectors to results
        )

        points = points_response[0]

        if not points:
            return JSONResponse(
                {
                    "success": True,
                    "results": [],
                    "coordinates_2d": [],
                    "message": "No vectors found for results",
                }
            )

        # Extract dense vectors (handle both named and unnamed vectors)
        def extract_dense_vector(point):
            if point.vector is None:
                return None
            # If named vectors (dict), extract "dense"
            if isinstance(point.vector, dict):
                return point.vector.get("dense")
            # If unnamed vector (array), use directly
            return point.vector

        vectors = np.array(
            [v for v in (extract_dense_vector(p) for p in points) if v is not None]
        )
        vector_fetch_duration = time.perf_counter() - vector_fetch_start

        if len(vectors) < 2:
            # Not enough points for PCA
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
                        }
                        for r in search_results
                    ],
                    "coordinates_2d": [[0, 0]] * len(search_results),
                    "message": "Not enough vectors for PCA",
                }
            )

        # Apply PCA dimensionality reduction (768-dim → 2D)
        pca_start = time.perf_counter()
        pca = PCA(n_components=2)
        coords_2d = pca.fit_transform(vectors)
        pca_duration = time.perf_counter() - pca_start

        # After fit, these attributes are guaranteed to be set
        assert pca.explained_variance_ratio_ is not None

        logger.info(
            f"PCA explained variance: PC1={pca.explained_variance_ratio_[0]:.3f}, "
            f"PC2={pca.explained_variance_ratio_[1]:.3f}"
        )

        # Map results to coordinates (use first chunk per document)
        result_coords = []
        seen_doc_ids = set()

        for point, coord in zip(points, coords_2d):
            if point.payload:
                doc_id = int(point.payload.get("doc_id", 0))
                if doc_id not in seen_doc_ids and doc_id in doc_ids:
                    seen_doc_ids.add(doc_id)
                    result_coords.append(coord.tolist())

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
            }
            for r in search_results
        ]

        # Calculate total request duration
        total_duration = time.perf_counter() - request_start

        # Log comprehensive timing metrics
        logger.info(
            f"Viz search timing: total={total_duration * 1000:.1f}ms, "
            f"search={search_duration * 1000:.1f}ms ({search_duration / total_duration * 100:.1f}%), "
            f"vector_fetch={vector_fetch_duration * 1000:.1f}ms ({vector_fetch_duration / total_duration * 100:.1f}%), "
            f"pca={pca_duration * 1000:.1f}ms ({pca_duration / total_duration * 100:.1f}%), "
            f"results={len(search_results)}, vectors={len(vectors)}"
        )

        return JSONResponse(
            {
                "success": True,
                "results": response_results,
                "coordinates_2d": result_coords[: len(search_results)],
                "pca_variance": {
                    "pc1": float(pca.explained_variance_ratio_[0]),
                    "pc2": float(pca.explained_variance_ratio_[1]),
                },
                "timing": {
                    "total_ms": round(total_duration * 1000, 2),
                    "search_ms": round(search_duration * 1000, 2),
                    "vector_fetch_ms": round(vector_fetch_duration * 1000, 2),
                    "pca_ms": round(pca_duration * 1000, 2),
                    "num_results": len(search_results),
                    "num_vectors": len(vectors),
                },
            }
        )

    except Exception as e:
        logger.error(f"Viz search error: {e}", exc_info=True)
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
        context_chars = int(request.query_params.get("context", "500"))

        # Validate required parameters
        if not all([doc_type, doc_id, start_str, end_str]):
            return JSONResponse(
                {
                    "success": False,
                    "error": "Missing required parameters: doc_type, doc_id, start, end",
                },
                status_code=400,
            )

        start = int(start_str)
        end = int(end_str)

        # Currently only support notes
        if doc_type != "note":
            return JSONResponse(
                {"success": False, "error": f"Unsupported doc_type: {doc_type}"},
                status_code=400,
            )

        # Get authenticated HTTP client and fetch note
        from nextcloud_mcp_server.auth.userinfo_routes import (
            _get_authenticated_client_for_userinfo,
        )
        from nextcloud_mcp_server.client.notes import NotesClient

        # Get username from request auth
        username = (
            request.user.display_name
            if hasattr(request.user, "display_name")
            else "unknown"
        )

        # Create notes client with authenticated HTTP client
        http_client = await _get_authenticated_client_for_userinfo(request)
        notes_client = NotesClient(http_client, username)

        # Fetch full note content
        note = await notes_client.get_note(int(doc_id))
        full_content = f"{note['title']}\n\n{note['content']}"

        # Validate offsets
        if start < 0 or end > len(full_content) or start >= end:
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Invalid offsets: start={start}, end={end}, content_length={len(full_content)}",
                },
                status_code=400,
            )

        # Extract chunk
        chunk_text = full_content[start:end]

        # Extract context before and after
        before_start = max(0, start - context_chars)
        before_context = full_content[before_start:start]

        after_end = min(len(full_content), end + context_chars)
        after_context = full_content[end:after_end]

        # Determine if there's more content
        has_more_before = before_start > 0
        has_more_after = after_end < len(full_content)

        logger.info(
            f"Fetched chunk context for {doc_type}_{doc_id}: "
            f"chunk_len={len(chunk_text)}, before_len={len(before_context)}, "
            f"after_len={len(after_context)}"
        )

        return JSONResponse(
            {
                "success": True,
                "chunk_text": chunk_text,
                "before_context": before_context,
                "after_context": after_context,
                "has_more_before": has_more_before,
                "has_more_after": has_more_after,
            }
        )

    except ValueError as e:
        logger.error(f"Invalid parameter format: {e}")
        return JSONResponse(
            {"success": False, "error": f"Invalid parameter format: {e}"},
            status_code=400,
        )
    except Exception as e:
        logger.error(f"Chunk context error: {e}", exc_info=True)
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )
