"""Visualization API endpoints for search and PDF preview.

ADR-018: Provides REST API endpoints for the Nextcloud PHP app (Astrolabe) to:
- Execute unified search with semantic/BM25/hybrid algorithms
- Execute vector search with PCA visualization coordinates
- Fetch chunk context with surrounding text
- Render PDF pages server-side (avoiding CSP/worker issues)

All endpoints require OAuth bearer token authentication via UnifiedTokenVerifier.
"""

import base64
import logging
from typing import Any

import pymupdf
from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.api.management import (
    _parse_float_param,
    _parse_int_param,
    _sanitize_error_for_client,
    _validate_query_string,
    validate_token_and_get_user,
)
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding.service import get_embedding_service
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
from nextcloud_mcp_server.vector.visualization import compute_pca_coordinates

logger = logging.getLogger(__name__)


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
        logger.warning("Unauthorized access to /api/v1/search: %s", e)
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
                if search_algo.query_embedding is not None:
                    query_embedding = search_algo.query_embedding
                else:
                    embedding_service = get_embedding_service()
                    query_embedding = await embedding_service.embed(query)

                pca_data = await compute_pca_coordinates(
                    paginated_results, query_embedding
                )
                response_data["pca_data"] = pca_data
            except Exception as e:
                logger.warning("Failed to compute PCA for unified search: %s", e)

        return JSONResponse(response_data)

    except Exception as e:
        logger.error("Error in unified search: %s", e)
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
        logger.warning("Unauthorized access to /api/v1/vector-viz/search: %s", e)
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
                # Get query embedding from search algorithm or generate it
                if search_algo.query_embedding is not None:
                    query_embedding = search_algo.query_embedding
                else:
                    embedding_service = get_embedding_service()
                    query_embedding = await embedding_service.embed(query)

                pca_data = await compute_pca_coordinates(all_results, query_embedding)
                response_data["coordinates_3d"] = pca_data["coordinates_3d"]
                response_data["query_coords"] = pca_data["query_coords"]
                if "pca_variance" in pca_data:
                    response_data["pca_variance"] = pca_data["pca_variance"]
            except Exception as e:
                logger.warning("Failed to compute PCA coordinates: %s", e)
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
        logger.warning("Unauthorized access to /api/v1/chunk-context: %s", e)
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

        # Type narrowing: we already checked these are not None above
        assert start_str is not None
        assert end_str is not None
        assert doc_id is not None
        assert doc_type is not None

        # Validate doc_id at the handler boundary: a malformed doc_id would
        # otherwise pass through to get_chunk_with_context and bottom out as a
        # 404 from deep inside, not a clear 400. Nextcloud IDs are unsigned
        # ints from MySQL auto_increment; doc_id stays a str downstream
        # (Qdrant payload index is keyword-typed). is_valid_nextcloud_doc_id
        # rejects "0", leading zeros, and Unicode digits that pass isdigit().
        #
        # Canonical TODO (referenced by ``auth/viz_routes.py`` and
        # ``vector/scanner.py:get_last_indexed_timestamp``): when chunk-
        # context support extends to non-numeric doc_types (calendar VEVENT
        # UIDs, CardDAV hrefs, …), relax this gate or make it doc_type-
        # aware. Today every indexed doc_type is numeric. The follow-up
        # tracker also covers the O(N) → O(1) migration of
        # ``get_last_indexed_timestamp`` (currently re-scans every
        # ``indexed_at`` on each tick).
        if not is_valid_nextcloud_doc_id(doc_id):
            return JSONResponse(
                {
                    "success": False,
                    "error": f"doc_id must be numeric, got {doc_id!r}",
                },
                status_code=400,
            )

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
            chunk_index: int | None = None
            if chunk_index_str is not None:
                chunk_index = _parse_int_param(
                    chunk_index_str, 0, 0, 1000000, "chunk_index"
                )
            total_chunks = _parse_int_param(
                total_chunks_str, 1, 1, 1000000, "total_chunks"
            )
        except ValueError as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=400)
        # doc_id is keyword-indexed in Qdrant as str — pass through verbatim
        # (no int coercion; producers always stringify on write).

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Use the user's stored app password for Nextcloud calls.
        # The OAuth bearer is only used to authenticate Astrolabe → MCP Server;
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

        if chunk_context is None:
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Failed to fetch chunk context for {doc_type} {doc_id}",
                },
                status_code=404,
            )

        # For PDF files, also fetch the chunk's bounding box from Qdrant if
        # available so the client can overlay a highlight on top of a
        # render-on-demand page image (Deck #76). Qdrant's page_number is
        # trusted over the context-expansion fallback when present.
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

        if chunk_bbox:
            response_data["chunk_bbox"] = chunk_bbox

        return JSONResponse(response_data)

    except Exception as e:
        error_msg = _sanitize_error_for_client(e, "get_chunk_context")
        return JSONResponse(
            {"error": error_msg},
            status_code=500,
        )


async def get_pdf_preview(request: Request) -> JSONResponse:
    """GET /api/v1/pdf-preview - Render PDF page to PNG image.

    Server-side PDF rendering using PyMuPDF. This endpoint allows Astrolabe
    to display PDF pages without requiring client-side PDF.js, avoiding CSP
    worker restrictions and ES private field issues in Chromium.

    Query parameters:
        file_path: WebDAV path to PDF file (e.g., "/Documents/report.pdf")
        page: Page number (1-indexed, default: 1)
        scale: Zoom factor for rendering (default: 2.0 = 144 DPI)

    Returns:
        {
            "success": true,
            "image": "<base64-encoded-png>",
            "page_number": 1,
            "total_pages": 10
        }

    Requires OAuth bearer token for authentication.
    """
    # Log incoming request
    file_path_param = request.query_params.get("file_path", "<not provided>")
    page_param = request.query_params.get("page", "1")
    logger.info(
        "PDF preview request: file_path=%s, page=%s", file_path_param, page_param
    )

    try:
        # Validate OAuth token and extract user
        user_id, validated = await validate_token_and_get_user(request)
        logger.info("PDF preview authenticated for user: %s", user_id)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/pdf-preview: %s", e)
        return JSONResponse(
            {
                "success": False,
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "get_pdf_preview"),
            },
            status_code=401,
        )

    try:
        # Parse and validate parameters
        file_path = request.query_params.get("file_path")
        if not file_path:
            return JSONResponse(
                {"success": False, "error": "Missing required parameter: file_path"},
                status_code=400,
            )

        # Validate no path traversal sequences
        if ".." in file_path:
            return JSONResponse(
                {"success": False, "error": "Invalid file path"},
                status_code=400,
            )

        try:
            page_num = _parse_int_param(
                request.query_params.get("page"), 1, 1, 10000, "page"
            )
            scale = _parse_float_param(
                request.query_params.get("scale"), 2.0, 0.5, 5.0, "scale"
            )
        except ValueError as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=400)

        # Get Nextcloud host from OAuth context
        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")

        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Use the user's stored app password for Nextcloud calls.
        # The OAuth bearer is only used to authenticate Astrolabe → MCP Server;
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
            pdf_bytes, _ = await nc_client.webdav.read_file(file_path)

        # Check file size limit (50 MB)
        max_pdf_size = 50 * 1024 * 1024
        if len(pdf_bytes) > max_pdf_size:
            return JSONResponse(
                {
                    "success": False,
                    "error": f"PDF file exceeds maximum size limit ({max_pdf_size // (1024 * 1024)} MB)",
                },
                status_code=413,
            )

        # Render page with PyMuPDF
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            total_pages = doc.page_count

            # Validate page number
            if page_num > total_pages:
                return JSONResponse(
                    {
                        "success": False,
                        "error": f"Page {page_num} does not exist (document has {total_pages} pages)",
                    },
                    status_code=400,
                )

            page = doc[page_num - 1]  # 0-indexed
            mat = pymupdf.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            png_bytes = pix.tobytes("png")
        finally:
            doc.close()

        # Encode as base64
        image_b64 = base64.b64encode(png_bytes).decode("ascii")

        logger.info(
            "Rendered PDF preview: %s page %s/%s, %s bytes",
            file_path,
            page_num,
            total_pages,
            format(len(png_bytes), ","),
        )

        return JSONResponse(
            {
                "success": True,
                "image": image_b64,
                "page_number": page_num,
                "total_pages": total_pages,
            }
        )

    except FileNotFoundError:
        logger.warning("PDF file not found: %s", file_path_param)
        return JSONResponse(
            {"success": False, "error": "PDF file not found"},
            status_code=404,
        )
    except (pymupdf.FileDataError, pymupdf.EmptyFileError):
        logger.warning("Invalid or corrupted PDF file: %s", file_path_param)
        return JSONResponse(
            {"success": False, "error": "Invalid or corrupted PDF file"},
            status_code=400,
        )
    except Exception as e:
        logger.error("PDF preview error: %s", e, exc_info=True)
        error_msg = _sanitize_error_for_client(e, "get_pdf_preview")
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=500,
        )
