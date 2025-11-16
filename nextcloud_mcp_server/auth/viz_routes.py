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

import numpy as np
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

    html_content = f"""
        <style>
            .viz-card {{
                background: white;
                border-radius: 8px;
                padding: 20px;
                margin-bottom: 20px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            .viz-controls {{
                margin-bottom: 20px;
            }}
            .viz-control-row {{
                display: grid;
                grid-template-columns: 2fr 1fr auto;
                gap: 12px;
                margin-bottom: 12px;
                align-items: end;
            }}
            .viz-control-group {{
                margin-bottom: 15px;
            }}
            .viz-control-group label {{
                display: block;
                margin-bottom: 5px;
                font-weight: 500;
                color: #333;
            }}
            .viz-control-group input[type="text"],
            .viz-control-group input[type="number"],
            .viz-control-group select {{
                width: 100%;
                padding: 8px 12px;
                border: 1px solid #ddd;
                border-radius: 4px;
                font-size: 14px;
            }}
            .viz-control-group input[type="range"] {{
                width: 100%;
            }}
            .viz-control-group select[multiple] {{
                min-height: 100px;
            }}
            .viz-weight-display {{
                display: inline-block;
                min-width: 40px;
                text-align: right;
                color: #666;
            }}
            .viz-btn {{
                background: #0066cc;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 4px;
                cursor: pointer;
                font-size: 14px;
                font-weight: 500;
            }}
            .viz-btn:hover {{
                background: #0052a3;
            }}
            .viz-btn-secondary {{
                background: #6c757d;
                color: white;
                border: none;
                padding: 6px 12px;
                border-radius: 4px;
                cursor: pointer;
                font-size: 13px;
                margin-bottom: 12px;
            }}
            .viz-btn-secondary:hover {{
                background: #5a6268;
            }}
            #viz-plot-container {{
                width: 100%;
                height: 600px;
                position: relative;
            }}
            #viz-plot {{
                width: 100%;
                height: 100%;
            }}
            .viz-loading {{
                text-align: center;
                padding: 40px;
                color: #666;
            }}
            .viz-loading-overlay {{
                position: absolute;
                inset: 0;
                display: flex;
                align-items: center;
                justify-content: center;
                background: white;
                color: #666;
            }}
            .viz-no-results {{
                text-align: center;
                padding: 40px;
                color: #666;
                font-style: italic;
            }}
            .viz-advanced-section {{
                margin-top: 16px;
                padding: 16px;
                background: #f8f9fa;
                border-radius: 4px;
                border: 1px solid #dee2e6;
            }}
            .viz-advanced-grid {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
            }}
            .viz-info-box {{
                background: #e3f2fd;
                border-left: 4px solid #2196f3;
                padding: 12px;
                margin-bottom: 20px;
                font-size: 14px;
            }}
        </style>

        <div x-data="vizApp()">
            <div class="viz-card">
                <h2>Vector Visualization</h2>
                <div class="viz-info-box">
                    Testing search algorithms on your indexed documents. User: <strong>{username}</strong>
                </div>

                <form @submit.prevent="executeSearch">
                    <div class="viz-controls">
                        <!-- Main Controls -->
                        <div class="viz-control-group">
                            <label>Search Query</label>
                            <input type="text" x-model="query" placeholder="Enter search query..." required />
                        </div>

                        <div class="viz-control-row">
                            <div class="viz-control-group" style="margin-bottom: 0;">
                                <label>Algorithm</label>
                                <select x-model="algorithm">
                                    <option value="semantic">Semantic (Dense Vectors)</option>
                                    <option value="bm25_hybrid" selected>BM25 Hybrid (Dense + Sparse RRF)</option>
                                </select>
                            </div>

                            <div style="display: flex; align-items: flex-end;">
                                <button type="submit" class="viz-btn" style="width: 100%;">Search & Visualize</button>
                            </div>

                            <div style="display: flex; align-items: flex-end;">
                                <button type="button" class="viz-btn-secondary" @click="showAdvanced = !showAdvanced" style="white-space: nowrap;">
                                    <span x-text="showAdvanced ? 'Hide Advanced' : 'Advanced'"></span>
                                </button>
                            </div>
                        </div>

                        <!-- Advanced Options (Collapsible) -->
                        <div class="viz-advanced-section" x-show="showAdvanced" x-transition.opacity.duration.200ms>
                            <h3 style="margin-top: 0; margin-bottom: 16px; font-size: 16px;">Advanced Options</h3>

                            <div class="viz-advanced-grid">
                                <div class="viz-control-group">
                                    <label>Document Types</label>
                                    <select x-model="docTypes" multiple>
                                        <option value="">All Types (cross-app search)</option>
                                        <option value="note">Notes</option>
                                        <option value="file">Files</option>
                                        <option value="calendar">Calendar Events</option>
                                        <option value="contact">Contacts</option>
                                        <option value="deck">Deck Cards</option>
                                    </select>
                                    <small style="color: #666; display: block; margin-top: 4px;">
                                        Hold Ctrl/Cmd to select multiple
                                    </small>
                                </div>

                                <div>
                                    <div class="viz-control-group">
                                        <label>Score Threshold (Semantic/Hybrid)</label>
                                        <input type="number" x-model.number="scoreThreshold" min="0" max="1" step="0.1" />
                                    </div>

                                    <div class="viz-control-group">
                                        <label>Result Limit</label>
                                        <input type="number" x-model.number="limit" min="1" max="100" />
                                    </div>
                                </div>
                            </div>

                            <!-- Info: BM25 Hybrid uses native RRF fusion (no manual weights) -->
                            <div x-show="algorithm === 'bm25_hybrid'" style="margin-top: 16px; padding: 12px; background: #e9ecef; border-radius: 4px;">
                                <p style="margin: 0; font-size: 14px; color: #666;">
                                    <strong>BM25 Hybrid Search:</strong> Uses Qdrant's native Reciprocal Rank Fusion (RRF)
                                    to automatically combine dense semantic vectors with sparse BM25 keyword vectors.
                                    No manual weight tuning required.
                                </p>
                            </div>
                        </div>
                    </div>
                </form>
            </div>

            <div class="viz-card">
                <div id="viz-plot-container">
                    <div x-show="loading" class="viz-loading-overlay" x-transition.opacity.duration.200ms>
                        Executing search and computing PCA projection...
                    </div>
                    <div id="viz-plot" x-show="!loading" x-transition.opacity.duration.200ms></div>
                </div>
            </div>

            <div class="viz-card">
                <h3>Search Results (<span x-text="loading ? '...' : results.length"></span>)</h3>

                <div x-show="loading" class="viz-loading" x-transition.opacity.duration.200ms>
                    Loading results...
                </div>

                <div x-show="!loading && results.length === 0" class="viz-no-results" x-transition.opacity.duration.200ms>
                    No results found. Try a different query or adjust your search parameters.
                </div>

                <template x-if="!loading && results.length > 0">
                    <div x-transition.opacity.duration.200ms>
                        <template x-for="result in results" :key="result.id">
                            <div style="padding: 12px; border-bottom: 1px solid #eee;">
                                <a :href="getNextcloudUrl(result)" target="_blank" style="font-weight: 500; color: #0066cc; text-decoration: none;">
                                    <span x-text="result.title"></span>
                                </a>
                                <div style="font-size: 14px; color: #666; margin-top: 4px;" x-text="result.excerpt"></div>
                                <div style="font-size: 12px; color: #999; margin-top: 4px;">
                                    Score: <span x-text="result.score.toFixed(3)"></span> |
                                    Type: <span x-text="result.doc_type"></span>
                                </div>
                            </div>
                        </template>
                    </div>
                </template>
            </div>
        </div>
    """

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

    # Parse doc_types (comma-separated list, None = all types)
    doc_types_param = request.query_params.get("doc_types", "")
    doc_types = doc_types_param.split(",") if doc_types_param else None

    logger.info(
        f"Viz search: user={username}, query='{query}', "
        f"algorithm={algorithm}, limit={limit}, doc_types={doc_types}"
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
                search_algo = BM25HybridSearchAlgorithm(score_threshold=score_threshold)
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

        # Normalize scores relative to this result set for better visualization
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

            # Rescale each result's score to 0-1 within this result set
            for r in search_results:
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
                "score": r.score,
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
