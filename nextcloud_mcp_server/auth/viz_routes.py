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

import numpy as np
from starlette.authentication import requires
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.search import (
    FuzzySearchAlgorithm,
    HybridSearchAlgorithm,
    KeywordSearchAlgorithm,
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

    # Get Nextcloud host for generating links to apps
    # Use public issuer URL if available (for browser-accessible links),
    # otherwise fall back to NEXTCLOUD_HOST
    import os

    nextcloud_host = os.getenv("NEXTCLOUD_PUBLIC_ISSUER_URL") or settings.nextcloud_host

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Vector Visualization - Nextcloud MCP</title>
        <script src="https://cdn.plot.ly/plotly-2.26.0.min.js"></script>
        <script src="https://unpkg.com/htmx.org@1.9.10"></script>
        <script src="https://unpkg.com/alpinejs@3.13.3/dist/cdn.min.js" defer></script>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                margin: 0;
                padding: 20px;
                background: #f5f5f5;
            }}
            .container {{
                max-width: 1400px;
                margin: 0 auto;
            }}
            .card {{
                background: white;
                border-radius: 8px;
                padding: 20px;
                margin-bottom: 20px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            .controls {{
                margin-bottom: 20px;
            }}
            .control-row {{
                display: grid;
                grid-template-columns: 2fr 1fr auto;
                gap: 12px;
                margin-bottom: 12px;
                align-items: end;
            }}
            .control-group {{
                margin-bottom: 15px;
            }}
            label {{
                display: block;
                margin-bottom: 5px;
                font-weight: 500;
                color: #333;
            }}
            input[type="text"], input[type="number"], select {{
                width: 100%;
                padding: 8px 12px;
                border: 1px solid #ddd;
                border-radius: 4px;
                font-size: 14px;
            }}
            input[type="range"] {{
                width: 100%;
            }}
            select[multiple] {{
                min-height: 100px;
            }}
            .weight-display {{
                display: inline-block;
                min-width: 40px;
                text-align: right;
                color: #666;
            }}
            .btn {{
                background: #0066cc;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 4px;
                cursor: pointer;
                font-size: 14px;
                font-weight: 500;
            }}
            .btn:hover {{
                background: #0052a3;
            }}
            .btn-secondary {{
                background: #6c757d;
                color: white;
                border: none;
                padding: 6px 12px;
                border-radius: 4px;
                cursor: pointer;
                font-size: 13px;
                margin-bottom: 12px;
            }}
            .btn-secondary:hover {{
                background: #5a6268;
            }}
            #plot {{
                width: 100%;
                height: 600px;
            }}
            .loading {{
                text-align: center;
                padding: 40px;
                color: #666;
            }}
            .advanced-section {{
                margin-top: 16px;
                padding: 16px;
                background: #f8f9fa;
                border-radius: 4px;
                border: 1px solid #dee2e6;
            }}
            .advanced-grid {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
            }}
            .info-box {{
                background: #e3f2fd;
                border-left: 4px solid #2196f3;
                padding: 12px;
                margin-bottom: 20px;
                font-size: 14px;
            }}
        </style>
    </head>
    <body>
        <div class="container" x-data="vizApp()">
            <div class="card">
                <h1>Vector Visualization</h1>
                <div class="info-box">
                    Testing search algorithms on your indexed documents. User: <strong>{username}</strong>
                </div>

                <form @submit.prevent="executeSearch">
                    <div class="controls">
                        <!-- Main Controls -->
                        <div class="control-group">
                            <label>Search Query</label>
                            <input type="text" x-model="query" placeholder="Enter search query..." required />
                        </div>

                        <div class="control-row">
                            <div class="control-group" style="margin-bottom: 0;">
                                <label>Algorithm</label>
                                <select x-model="algorithm">
                                    <option value="semantic">Semantic (Vector Similarity)</option>
                                    <option value="keyword">Keyword (Token Matching)</option>
                                    <option value="fuzzy">Fuzzy (Character Overlap)</option>
                                    <option value="hybrid" selected>Hybrid (RRF Fusion)</option>
                                </select>
                            </div>

                            <div style="display: flex; align-items: flex-end;">
                                <button type="submit" class="btn" style="width: 100%;">Search & Visualize</button>
                            </div>

                            <div style="display: flex; align-items: flex-end;">
                                <button type="button" class="btn-secondary" @click="showAdvanced = !showAdvanced" style="white-space: nowrap;">
                                    <span x-text="showAdvanced ? 'Hide Advanced' : 'Advanced'"></span>
                                </button>
                            </div>
                        </div>

                        <!-- Advanced Options (Collapsible) -->
                        <div class="advanced-section" x-show="showAdvanced" x-transition.opacity.duration.200ms>
                            <h3 style="margin-top: 0; margin-bottom: 16px; font-size: 16px;">Advanced Options</h3>

                            <div class="advanced-grid">
                                <div class="control-group">
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
                                    <div class="control-group">
                                        <label>Score Threshold (Semantic/Hybrid)</label>
                                        <input type="number" x-model.number="scoreThreshold" min="0" max="1" step="0.1" />
                                    </div>

                                    <div class="control-group">
                                        <label>Result Limit</label>
                                        <input type="number" x-model.number="limit" min="1" max="100" />
                                    </div>
                                </div>
                            </div>

                            <!-- Hybrid Weights (only when hybrid selected) -->
                            <div x-show="algorithm === 'hybrid'" style="margin-top: 16px; padding: 12px; background: #e9ecef; border-radius: 4px;">
                                <label style="margin-bottom: 12px; display: block;">Hybrid Algorithm Weights</label>

                                <div style="margin-bottom: 8px;">
                                    <label style="display: inline-block; width: 100px; font-weight: normal;">Semantic:</label>
                                    <input type="range" x-model.number="semanticWeight" min="0" max="1" step="0.1" style="width: 200px; display: inline-block;">
                                    <span class="weight-display" x-text="semanticWeight.toFixed(1)"></span>
                                </div>
                                <div style="margin-bottom: 8px;">
                                    <label style="display: inline-block; width: 100px; font-weight: normal;">Keyword:</label>
                                    <input type="range" x-model.number="keywordWeight" min="0" max="1" step="0.1" style="width: 200px; display: inline-block;">
                                    <span class="weight-display" x-text="keywordWeight.toFixed(1)"></span>
                                </div>
                                <div>
                                    <label style="display: inline-block; width: 100px; font-weight: normal;">Fuzzy:</label>
                                    <input type="range" x-model.number="fuzzyWeight" min="0" max="1" step="0.1" style="width: 200px; display: inline-block;">
                                    <span class="weight-display" x-text="fuzzyWeight.toFixed(1)"></span>
                                </div>
                            </div>
                        </div>
                    </div>
                </form>
            </div>

            <div class="card">
                <div x-show="loading" class="loading">
                    Executing search and computing PCA projection...
                </div>
                <div id="plot" x-show="!loading"></div>
            </div>

            <div class="card" x-show="results.length > 0">
                <h2>Search Results (<span x-text="results.length"></span>)</h2>
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
        </div>

        <script>
            function vizApp() {{
                return {{
                    query: '',
                    algorithm: 'hybrid',
                    showAdvanced: false,
                    docTypes: [''],  // Default to "All Types"
                    limit: 50,
                    scoreThreshold: 0.7,
                    semanticWeight: 0.5,
                    keywordWeight: 0.3,
                    fuzzyWeight: 0.2,
                    loading: false,
                    results: [],

                    async executeSearch() {{
                        this.loading = true;
                        this.results = [];

                        try {{
                            const params = new URLSearchParams({{
                                query: this.query,
                                algorithm: this.algorithm,
                                limit: this.limit,
                                score_threshold: this.scoreThreshold,
                                semantic_weight: this.semanticWeight,
                                keyword_weight: this.keywordWeight,
                                fuzzy_weight: this.fuzzyWeight,
                            }});

                            // Add doc_types parameter (filter out empty string for "All Types")
                            const selectedTypes = this.docTypes.filter(t => t !== '');
                            if (selectedTypes.length > 0) {{
                                params.append('doc_types', selectedTypes.join(','));
                            }}

                            const response = await fetch(`/app/vector-viz/search?${{params}}`);
                            const data = await response.json();

                            if (data.success) {{
                                this.results = data.results;
                                this.renderPlot(data.coordinates_2d, data.results);
                            }} else {{
                                alert('Search failed: ' + data.error);
                            }}
                        }} catch (error) {{
                            alert('Error: ' + error.message);
                        }} finally {{
                            this.loading = false;
                        }}
                    }},

                    renderPlot(coordinates, results) {{
                        const trace = {{
                            x: coordinates.map(c => c[0]),
                            y: coordinates.map(c => c[1]),
                            mode: 'markers',
                            type: 'scatter',
                            text: results.map(r => `${{r.title}}<br>Score: ${{r.score.toFixed(3)}}`),
                            marker: {{
                                size: 8,
                                color: results.map(r => r.score),
                                colorscale: 'Viridis',
                                showscale: true,
                                colorbar: {{ title: 'Score' }}
                            }}
                        }};

                        const layout = {{
                            title: `Vector Space (PCA 2D) - ${{results.length}} results`,
                            xaxis: {{ title: 'PC1' }},
                            yaxis: {{ title: 'PC2' }},
                            hovermode: 'closest',
                            height: 600
                        }};

                        Plotly.newPlot('plot', [trace], layout);
                    }},

                    getNextcloudUrl(result) {{
                        // Generate Nextcloud URL based on document type
                        // Use the actual Nextcloud host (port 8080), not the MCP server
                        const baseUrl = '{nextcloud_host}';

                        switch (result.doc_type) {{
                            case 'note':
                                return `${{baseUrl}}/apps/notes/note/${{result.id}}`;
                            case 'file':
                                return `${{baseUrl}}/apps/files/?fileId=${{result.id}}`;
                            case 'calendar':
                                return `${{baseUrl}}/apps/calendar`;
                            case 'contact':
                                return `${{baseUrl}}/apps/contacts`;
                            case 'deck':
                                return `${{baseUrl}}/apps/deck`;
                            default:
                                return `${{baseUrl}}`;
                        }}
                    }}
                }}
            }}
        </script>
    </body>
    </html>
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
    algorithm = request.query_params.get("algorithm", "hybrid")
    limit = int(request.query_params.get("limit", "50"))
    score_threshold = float(request.query_params.get("score_threshold", "0.7"))
    semantic_weight = float(request.query_params.get("semantic_weight", "0.5"))
    keyword_weight = float(request.query_params.get("keyword_weight", "0.3"))
    fuzzy_weight = float(request.query_params.get("fuzzy_weight", "0.2"))

    # Parse doc_types (comma-separated list, None = all types)
    doc_types_param = request.query_params.get("doc_types", "")
    doc_types = doc_types_param.split(",") if doc_types_param else None

    logger.info(
        f"Viz search: user={username}, query='{query}', "
        f"algorithm={algorithm}, limit={limit}, doc_types={doc_types}"
    )

    try:
        # Get authenticated HTTP client from session
        # In BasicAuth mode: uses username/password from session
        # In OAuth mode: uses access token from session
        from nextcloud_mcp_server.auth.userinfo_routes import (
            _get_authenticated_client_for_userinfo,
        )
        from nextcloud_mcp_server.client.notes import NotesClient

        async with await _get_authenticated_client_for_userinfo(request) as http_client:
            # Create NotesClient directly with authenticated HTTP client
            notes_client = NotesClient(http_client, username)

            # Wrap in a minimal client object for search algorithms
            # This conforms to NextcloudClientProtocol but only implements notes
            class MinimalNextcloudClient:
                def __init__(self, notes_client, username):
                    self._notes = notes_client
                    self.username = username

                @property
                def notes(self):
                    return self._notes

                @property
                def webdav(self):
                    return None

                @property
                def calendar(self):
                    return None

                @property
                def contacts(self):
                    return None

                @property
                def deck(self):
                    return None

                @property
                def cookbook(self):
                    return None

                @property
                def tables(self):
                    return None

            nextcloud_client = MinimalNextcloudClient(notes_client, username)

            # Create search algorithm
            if algorithm == "semantic":
                search_algo = SemanticSearchAlgorithm(score_threshold=score_threshold)
            elif algorithm == "keyword":
                search_algo = KeywordSearchAlgorithm()
            elif algorithm == "fuzzy":
                search_algo = FuzzySearchAlgorithm()
            elif algorithm == "hybrid":
                search_algo = HybridSearchAlgorithm(
                    semantic_weight=semantic_weight,
                    keyword_weight=keyword_weight,
                    fuzzy_weight=fuzzy_weight,
                )
            else:
                return JSONResponse(
                    {"success": False, "error": f"Unknown algorithm: {algorithm}"},
                    status_code=400,
                )

            # Execute search (supports cross-app when doc_types=None)
            if doc_types is None or len(doc_types) == 0:
                # Cross-app search - search all indexed types
                search_results = await search_algo.search(
                    query=query,
                    user_id=username,
                    limit=limit,
                    doc_type=None,  # Search all types
                    nextcloud_client=nextcloud_client,
                    score_threshold=score_threshold,
                )
            elif len(doc_types) == 1:
                # Single document type
                search_results = await search_algo.search(
                    query=query,
                    user_id=username,
                    limit=limit,
                    doc_type=doc_types[0],
                    nextcloud_client=nextcloud_client,
                    score_threshold=score_threshold,
                )
            else:
                # Multiple document types - search each and combine
                all_results = []
                for doc_type in doc_types:
                    results = await search_algo.search(
                        query=query,
                        user_id=username,
                        limit=limit * 2,  # Get extra per type
                        doc_type=doc_type,
                        nextcloud_client=nextcloud_client,
                        score_threshold=score_threshold,
                    )
                    all_results.extend(results)

                # Sort by score and limit
                all_results.sort(key=lambda r: r.score, reverse=True)
                search_results = all_results[:limit]

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
            with_vectors=True,
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

        # Extract vectors
        vectors = np.array([p.vector for p in points if p.vector is not None])

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
        pca = PCA(n_components=2)
        coords_2d = pca.fit_transform(vectors)

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

        return JSONResponse(
            {
                "success": True,
                "results": response_results,
                "coordinates_2d": result_coords[: len(search_results)],
                "pca_variance": {
                    "pc1": float(pca.explained_variance_ratio_[0]),
                    "pc2": float(pca.explained_variance_ratio_[1]),
                },
            }
        )

    except Exception as e:
        logger.error(f"Viz search error: {e}", exc_info=True)
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )
