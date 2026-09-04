"""Visualization API endpoints for search and chunk context.

ADR-018: Provides REST API endpoints for the Nextcloud PHP app (Astrolabe) to:
- Execute unified search with semantic/BM25/hybrid algorithms
- Execute vector search with PCA visualization coordinates
- Fetch chunk context with surrounding text

None of these read file content: chunk bboxes and page numbers come from the
Qdrant payload, and Astrolabe rasterizes PDF pages in the browser from the copy
already in Nextcloud. See tests/unit/test_api_no_whole_file_reads.py.

All endpoints require OAuth bearer token authentication via UnifiedTokenVerifier.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.api.management import (
    UnsupportedSearchType,
    _parse_float_param,
    _parse_int_param,
    _sanitize_error_for_client,
    _validate_query_string,
    select_search_algorithm,
    validate_token_and_get_user,
)
from nextcloud_mcp_server.config import Settings, get_settings
from nextcloud_mcp_server.observability.metrics import (
    record_search_request,
    record_search_stage,
)
from nextcloud_mcp_server.providers import get_provider
from nextcloud_mcp_server.search import (
    GRANULARITY_CHUNK,
    GRANULARITY_DOCUMENT,
    VALID_GRANULARITIES,
    BM25HybridSearchAlgorithm,
    SearchAlgorithm,
    SemanticSearchAlgorithm,
)
from nextcloud_mcp_server.search.access_filter import (
    AccessibleScope,
    list_accessible_owners,
    list_accessible_scope,
    normalize_path_prefixes,
)
from nextcloud_mcp_server.search.bm25_hybrid import search_method_label
from nextcloud_mcp_server.search.context import (
    get_chunk_bbox_and_page_from_qdrant,
    get_chunk_with_context,
)
from nextcloud_mcp_server.search.relevance import (
    RELEVANCE_ORDINAL,
    filter_by_relevance,
    relevance_fit_base_rate,
    relevance_for,
)
from nextcloud_mcp_server.search.rerank import (
    RERANK_APPLIED,
    RERANK_DEGRADED,
    RERANK_SKIPPED,
    effective_pool_size,
    rerank_available,
    rerank_results,
)
from nextcloud_mcp_server.search.verification import verify_search_results
from nextcloud_mcp_server.usage.search import record_search_usage
from nextcloud_mcp_server.utils.validation import (
    is_valid_nextcloud_doc_id,
    parse_modified_timestamp,
)
from nextcloud_mcp_server.vector.oauth_sync import (
    NotProvisionedError,
    get_user_client_basic_auth,
)
from nextcloud_mcp_server.vector.visualization import compute_pca_coordinates

logger = logging.getLogger(__name__)

_NEXTCLOUD_HOST_NOT_CONFIGURED = "Nextcloud host not configured"


def _search_algorithm_label(algorithm: str, fusion: str) -> str:
    """The ``algorithm`` label for search metrics.

    Shared by every ``record_search_request`` call site so success and error
    samples land on the SAME series. Normalising in one place and using the raw
    value in another fragments ``astrolabe_search_requests_total`` into
    non-comparable series and quietly breaks "error rate by algorithm" — exactly
    the surface drift these metrics exist to prevent.

    Matches the MCP tool's ``search_method`` convention
    (``bm25_hybrid_<fusion>``) so the two entrypoints stay comparable.
    """
    if algorithm == "semantic":
        return algorithm
    # Shared helper: clamps an unrecognised fusion so a caller-supplied string
    # can never reach a Prometheus label from either surface.
    return search_method_label(fusion)


def _unsupported_search_type_response(e: UnsupportedSearchType) -> JSONResponse:
    """Uniform 422 for an explicit unsupported search algorithm.

    Shared by both search endpoints so the ``unsupported_search_type`` payload
    shape (error / requested / supported_search_types) can't drift between them.
    """
    return JSONResponse(
        {
            "error": "unsupported_search_type",
            "requested": e.requested,
            "supported_search_types": e.supported,
        },
        status_code=422,
    )


def _parse_rerank(body: dict[str, Any]) -> tuple[bool, JSONResponse | None]:
    """Read the optional ``rerank`` request flag (shape only).

    Returns ``(rerank, error_response)``; the caller returns the response when it
    is not None.

    A non-boolean is a **400** — malformed. Coerced truthiness would let
    ``"false"`` turn reranking ON, and a caller that asked for reranked ordering
    and silently got retrieval ordering cannot tell that apart from a ranking
    regression.

    Deliberately SEPARATE from :func:`_rerank_capability_error`, which the caller
    invokes later. The two checks are not interchangeable in position: this one
    validates the request body alongside the other body parsing, while the
    capability gate belongs after the query and algorithm checks so that a
    request failing several validations reports the same error it always did.
    Folding them into one call moved the 422 ahead of the empty-query
    short-circuit and changed which error an empty query + unconfigured rerank
    returns. Pinned by ``test_search_rerank_api.py`` precedence tests.
    """
    rerank = body.get("rerank", False)
    if not isinstance(rerank, bool):
        return False, JSONResponse(
            {"error": f"Invalid rerank {rerank!r}. Must be a boolean"},
            status_code=400,
        )
    return rerank, None


def _rerank_capability_error(rerank: bool, settings: Settings) -> JSONResponse | None:
    """422 when reranking was asked for on a deployment that cannot serve it.

    A **422** rather than 400: the request is well-formed, this server just
    cannot serve it. Callers discover the capability from ``rerank_available``
    on ``GET /api/v1/status`` rather than probing for the error.

    Call this AFTER the empty-query and algorithm/granularity checks — see
    :func:`_parse_rerank` for why the position is load-bearing.
    """
    if rerank and not rerank_available(settings):
        return JSONResponse(
            {
                "error": "rerank_not_configured",
                "message": (
                    "Reranking is not configured on this server. Check "
                    "`rerank_available` on /api/v1/status before requesting it."
                ),
            },
            status_code=422,
        )
    return None


def _reranked_label(rerank: bool, outcome: str) -> str:
    """Metric label for the ordering a response actually carries.

    Three distinct states, not a boolean: never asked for, asked for and applied,
    asked for and degraded. Collapsing the last two would hide a reranker outage
    behind the same label as "not requested".

    SKIPPED (nothing to reorder) reports as ``"false"``, not ``"unavailable"``: a
    query returning 0-1 rows is routine, and folding it into the outage bucket
    would bury real reranker failures in noise.
    """
    if not rerank:
        return "false"
    if outcome == RERANK_APPLIED:
        return "true"
    if outcome == RERANK_DEGRADED:
        return "unavailable"
    return "false"


def _rerank_sort_key(result: Any) -> tuple[bool, float]:
    """Order reranked results, keeping unscored rows at the tail.

    Rows the reranker did not score sort behind every scored row rather than
    being compared against them. They are different scales — a cross-encoder
    score spans [0, 1] while ``.score`` is a rank artifact (~2/k for RRF) or an
    unbounded raw BM25 value — so ranking them against each other lets an
    UNSCORED row outrank a genuinely reranked one purely by scale (a raw BM25
    8.5 beats every possible rerank score). Unscored rows are exactly the ones
    ``rerank_results`` appends in retrieval order to sit at the tail, so this
    preserves that placement instead of shuffling them back into the middle.
    """
    return (
        result.rerank_score is not None,
        result.rerank_score if result.rerank_score is not None else result.score,
    )


def _build_search_algorithm(
    requested_algorithm: str | None,
    settings: Settings,
    *,
    score_threshold: float,
    fusion: str,
) -> tuple[SearchAlgorithm, str, str]:
    """Resolve + instantiate the search algorithm for a request.

    Shared by both search endpoints (`/api/v1/search`, `/api/v1/vector-viz/search`)
    so their selection logic can't drift. Raises :class:`UnsupportedSearchType`
    for an *explicit* unsupported algorithm (the caller maps it to a 422 via
    :func:`_unsupported_search_type_response`); an absent algorithm defaults
    gracefully. Returns ``(algorithm instance, resolved algorithm name,
    normalized fusion)``.

    **The normalized fusion is returned, not just used internally.** It used to
    be a local, so callers kept passing the raw request value to everything
    downstream — metrics, usage, and the relevance mapping — while retrieval ran
    on the normalized one. That let ``{"fusion": null}`` (``.get`` returns None
    when the key is present) or any typo produce a correct search whose response
    reported ``relevance`` from the *uncalibrated* branch: the raw ~0.03 RRF
    score, i.e. the exact "3%" bug ADR-034 exists to remove, reachable through a
    malformed-but-plausible body. Callers must use the returned value.
    """
    algorithm = select_search_algorithm(requested_algorithm, settings)
    # Normalize before the branch so every caller gets a usable value, including
    # the dense-only path where fusion is moot but still reaches metric labels.
    fusion = fusion if fusion in ("rrf", "dbsf") else "rrf"
    if algorithm == "semantic":
        return (
            SemanticSearchAlgorithm(score_threshold=score_threshold),
            algorithm,
            fusion,
        )
    # Both "bm25" and "hybrid" run BM25HybridSearchAlgorithm — it fuses dense
    # semantic + sparse BM25; keyword-only documents contribute via the sparse
    # side of the same query.
    return (
        BM25HybridSearchAlgorithm(score_threshold=score_threshold, fusion=fusion),
        algorithm,
        fusion,
    )


async def _search_with_acl(
    request: Request,
    user_id: str,
    execute: Callable[[AccessibleScope | None], Awaitable[list]],
) -> tuple[list, int]:
    """Resolve the caller's Nextcloud client, run ``execute(scope)``, and
    verify-on-read — shared by the /api/v1 search endpoints.

    The OAuth bearer only authenticates Astrolabe → MCP Server; MCP Server →
    Nextcloud uses the provisioned app password. When the caller never
    provisioned background sync there is no client to expand shares or verify
    with, so we fall back to self-only, unverified search (the pre-ACL
    behaviour) rather than 401 — keeping search working for users who haven't
    opted into background indexing.

    Args:
        request: The Starlette request (carries ``app.state.oauth_context``).
        user_id: The authenticated caller.
        execute: Coroutine that runs the search for a given access scope
            (``None`` ⇒ self-only).

    Returns:
        ``(results, dropped)`` — the result list (verified for provisioned
        callers) and the number of documents verify-on-read removed. ``dropped``
        is always 0 on the unprovisioned path, where no verification runs; the
        caller records it as a metric, so the two search surfaces report the
        same ghost-record signal.

    Raises:
        ValueError: If the Nextcloud host is not configured.
    """
    oauth_ctx = request.app.state.oauth_context
    nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")
    if not nextcloud_host:
        raise ValueError(_NEXTCLOUD_HOST_NOT_CONFIGURED)

    # Stage timings are recorded here rather than around this whole call so
    # "retrieve" means embed+Qdrant and "verify" means the Nextcloud round-trips
    # — timing the wrapper would conflate them into one unattributable number.
    dropped = 0
    try:
        nc_client = await get_user_client_basic_auth(user_id, nextcloud_host)
    except NotProvisionedError:
        logger.debug("User %s not provisioned; self-only unverified search", user_id)
        retrieve_start = anyio.current_time()
        results = await execute(None)
        record_search_stage("http", "retrieve", anyio.current_time() - retrieve_start)
    else:
        async with nc_client:
            # Expand to owners who shared content with the caller (same as the
            # MCP tool path) so shared documents are searchable.
            scope = await list_accessible_scope(nc_client.sharing, user_id)
            retrieve_start = anyio.current_time()
            results = await execute(scope)
            record_search_stage(
                "http", "retrieve", anyio.current_time() - retrieve_start
            )
            # Verify-on-read (ADR-019): drop documents the caller can no longer
            # access (e.g. a revoked share). Eviction runs inline — this
            # Starlette route has no MCPServer lifespan task group.
            verify_start = anyio.current_time()
            results, dropped = await verify_search_results(nc_client, results)
            record_search_stage("http", "verify", anyio.current_time() - verify_start)

    # Safe to log titles now: provisioned callers passed verify-on-read;
    # non-provisioned ran self-only (unverified titles are never logged — see
    # the search algorithms).
    if results:
        logger.debug(
            "Top verified results: %s",
            ", ".join(
                f"{r.doc_type}_{r.id} (score={r.score:.3f}, title='{r.title}')"
                for r in results[:5]
            ),
        )
    return results, dropped


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
        "include_chunks": true,  // include text snippets
        "granularity": "chunk"  // "chunk" (default) or "document": one row
                                // per document (its best chunk), so `limit`
                                // counts documents. "document" requires the
                                // bm25/hybrid algorithm.
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

    # Bound before the try so the error-path metric can label the request even
    # when parsing is what failed. Reading them out of locals() in the handler
    # would silently mislabel as "unknown" the moment either name moves.
    algorithm = "unknown"
    granularity = GRANULARITY_CHUNK
    fusion = "rrf"

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

            # No upper bound: hybrid DBSF fusion can exceed 1.0, so a le=1.0 cap
            # would 400 a legitimate threshold — mirrors the round-1
            # Field(ge=0.0) fix on the nc_semantic_search tool.
            score_threshold = _parse_float_param(
                body.get("score_threshold"),
                0.0,
                0.0,
                float("inf"),
                "score_threshold",
            )
            # Bounded, unlike score_threshold above: `relevance` is a mapped
            # [0, 1] value by construction, so anything outside that range is a
            # caller mistake rather than a legitimate threshold.
            min_relevance = _parse_float_param(
                body.get("min_relevance"), 0.0, 0.0, 1.0, "min_relevance"
            )

            # ADR-027 modified-date range filter. Accepts RFC 3339 / ISO 8601
            # datetimes or Unix seconds; normalized to int Unix seconds for the
            # numeric Range filter. Absent bound ⇒ open-ended.
            modified_after = parse_modified_timestamp(
                body.get("modified_after"), param_name="modified_after"
            )
            modified_before = parse_modified_timestamp(
                body.get("modified_before"), param_name="modified_before"
            )
            if (
                modified_after is not None
                and modified_before is not None
                and modified_after > modified_before
            ):
                raise ValueError("modified_after must be <= modified_before")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        requested_algorithm = body.get("algorithm")  # None ⇒ graceful default
        fusion = body.get("fusion", "rrf")
        # Unlike ``fusion`` (normalized to "rrf" when unrecognized), an
        # unrecognized granularity is rejected rather than silently downgraded:
        # a caller asking for one row per document and receiving several chunks
        # of the same document would look like a ranking bug, not a bad request.
        granularity = body.get("granularity", GRANULARITY_CHUNK)
        if granularity not in VALID_GRANULARITIES:
            return JSONResponse(
                {
                    "error": (
                        f"Invalid granularity {granularity!r}. "
                        f"Must be one of {list(VALID_GRANULARITIES)}"
                    )
                },
                status_code=400,
            )
        # Optional cross-encoder rerank. Shape checked here with the rest of the
        # body, for the same reason as granularity: a caller that asked for
        # reranked ordering and silently got retrieval ordering cannot tell the
        # difference from a ranking regression. The CAPABILITY gate runs later —
        # see _rerank_capability_error.
        rerank, rerank_error = _parse_rerank(body)
        if rerank_error is not None:
            return rerank_error
        include_pca = body.get("include_pca", False)
        include_chunks = body.get("include_chunks", True)
        doc_types = body.get("doc_types")  # Optional filter
        # ADR-027 Phase 2 path filter (files only); blank ⇒ no filter. Accept a
        # path_prefixes list (multi-folder) alongside the legacy single
        # path_prefix; normalize drops blanks and de-dupes.
        # path_prefixes arrives as a JSON array (the Astrolabe PHP client sends
        # a list); any other shape is ignored rather than guessed at. The legacy
        # single path_prefix is folded in by normalize_path_prefixes.
        _path_prefixes_raw = body.get("path_prefixes")
        path_prefixes = normalize_path_prefixes(
            body.get("path_prefix"),
            _path_prefixes_raw if isinstance(_path_prefixes_raw, list) else None,
        )

        if not query:
            return JSONResponse({"results": [], "total_found": 0})

        # Resolve + build the search algorithm: an *explicit* unsupported request
        # (e.g. any algorithm while vector sync is disabled) is rejected with 422
        # carrying the advertised supported_search_types, so the client can
        # correct it rather than silently receive fallback results. An absent
        # algorithm still defaults gracefully.
        try:
            search_algo, algorithm, fusion = _build_search_algorithm(
                requested_algorithm,
                settings,
                score_threshold=score_threshold,
                fusion=fusion,
            )
        except UnsupportedSearchType as e:
            return _unsupported_search_type_response(e)

        # Only the hybrid algorithm implements grouping. The dense-only path
        # would accept the kwarg and silently ignore it, returning several
        # chunks of one document to a caller that asked for one row per
        # document — indistinguishable from a ranking bug. Reject instead.
        # Astrolabe requests "hybrid" (its default), so this combination is
        # reachable only by an explicit opt-in to the dense algorithm.
        if granularity == GRANULARITY_DOCUMENT and algorithm == "semantic":
            return JSONResponse(
                {
                    "error": "granularity_unsupported_for_algorithm",
                    "granularity": granularity,
                    "algorithm": algorithm,
                    "supported_algorithms": ["bm25", "hybrid"],
                },
                status_code=422,
            )

        # Capability gate, in the position it has always occupied: after the
        # empty-query short-circuit and the algorithm/granularity check, so a
        # request that fails several validations at once reports the same error
        # it reported before the shared helper existed.
        rerank_error = _rerank_capability_error(rerank, settings)
        if rerank_error is not None:
            return rerank_error

        # Request extra results to handle offset
        search_limit = limit + offset
        # The budget an UNRERANKED request would have used, which differs by
        # retrieval branch: the doc_types loop over-fetches 2x before
        # verify-on-read, the single-query branch does not. total_found is
        # capped back to this so enabling reranking cannot change it — a flat
        # cap would under-report the doc_types path by half.
        unreranked_budget = (
            search_limit * 2
            if (doc_types and isinstance(doc_types, list))
            else search_limit
        )
        rerank_outcome = RERANK_SKIPPED
        # Reranking needs a deeper candidate pool than pagination alone; it is
        # offset-INDEPENDENT so page 2 reranks the same pool as page 1, which is
        # what keeps items from migrating across page boundaries. Pagination
        # beyond the pool therefore returns nothing, consistent with total_found.
        rerank_pool = (
            effective_pool_size(
                settings,
                floor=search_limit,
                grouped=granularity == GRANULARITY_DOCUMENT,
            )
            if rerank
            else search_limit
        )

        async def _execute(scope: AccessibleScope | None) -> list:
            """Run the search across requested doc_types with the given access
            scope (None ⇒ self-only)."""
            owners = scope.owners if scope else None
            # Narrow the owner branch to the subtrees actually shared with the
            # caller; see access_filter.build_ownership_filter.
            roots = scope.share_root_ids if scope else None
            results: list = []
            if doc_types and isinstance(doc_types, list):
                for doc_type in doc_types:
                    if doc_type:
                        results.extend(
                            await search_algo.search(
                                query=query,
                                user_id=user_id,
                                limit=rerank_pool,
                                doc_type=doc_type,
                                accessible_owners=owners,
                                shared_root_ids=roots,
                                granularity=granularity,
                                modified_after=modified_after,
                                modified_before=modified_before,
                                path_prefixes=path_prefixes,
                            )
                        )
                # Sort, then cap to a fixed over-fetch budget before the result
                # reaches verify-on-read. Without this, N doc_types each fetched
                # at search_limit would send N*search_limit candidates into
                # verification — one Nextcloud round-trip each — scaling the cost
                # with len(doc_types). 2x leaves headroom for verify-on-read
                # drops before pagination, matching the nc_semantic_search
                # pattern.
                results.sort(key=lambda r: r.score, reverse=True)
                results = results[: max(search_limit * 2, rerank_pool)]
            else:
                results = await search_algo.search(
                    query=query,
                    user_id=user_id,
                    limit=rerank_pool,
                    accessible_owners=owners,
                    shared_root_ids=roots,
                    granularity=granularity,
                    modified_after=modified_after,
                    modified_before=modified_before,
                    path_prefixes=path_prefixes,
                )
            # Rerank inside the closure, i.e. BEFORE verify-on-read runs in
            # _search_with_acl, and after the per-doc_type merge so one pass
            # covers every type.
            nonlocal rerank_outcome
            if rerank:
                results, rerank_outcome = await rerank_results(
                    results, query, settings=settings, surface="http"
                )
                # Cut back to the budget an unreranked request would have used
                # BEFORE this returns into verify-on-read. The deep pool exists
                # for the reranker, not for verification — without this, a
                # provisioned caller sends the whole pool through
                # verify_search_results, which is one Nextcloud round-trip per
                # candidate. That turns enabling reranking into an order-of-
                # magnitude increase in load on Nextcloud, which is exactly the
                # trade the rerank-before-verify ordering was chosen to avoid.
                results = results[:unreranked_budget]
            return results

        all_results, dropped_count = await _search_with_acl(request, user_id, _execute)

        # Sort by rerank score when present, retrieval score otherwise —
        # without this the re-sort would silently undo the rerank ordering,
        # since .score deliberately still holds the retrieval score. See
        # _rerank_sort_key for why unscored rows are kept at the tail rather
        # than compared against reranked ones.
        sorted_results = sorted(all_results, key=_rerank_sort_key, reverse=True)

        # Relevance cut, applied BEFORE pagination so a page fills with rows
        # that qualify rather than returning a short page of whatever survived.
        # Distinct from score_threshold, which Qdrant applies to the raw
        # retrieval score before reranking even runs.
        sorted_results = filter_by_relevance(
            sorted_results,
            min_relevance=min_relevance,
            fusion=fusion,
            algorithm=algorithm,
            rerank_model=settings.search_rerank_model,
        )

        # Calculate total and apply pagination.
        #
        # The rerank pool must NOT leak into total_found. Without reranking this
        # counts a pool sized by `limit + offset`; a rerank pool is far deeper,
        # so reporting its length would multiply the page count a client derives
        # from this field — reshaping Astrolabe's pager with no client change
        # and no version signal, which is exactly the kind of silent
        # cross-service break the repo's version-gating rule exists to prevent.
        # Cap it back to the budget an unreranked request would have used, so
        # turning reranking on changes result ORDER and nothing else.
        total_found = len(sorted_results)
        if rerank:
            total_found = min(total_found, unreranked_budget)
        paginated_results = sorted_results[offset : offset + limit]

        # Format results for Unified Search
        formatted_results = []
        for result in paginated_results:
            # Get document ID (prefer note_id for notes)
            doc_id = result.id
            if result.metadata and "note_id" in result.metadata:
                doc_id = result.metadata["note_id"]

            relevance, relevance_source = relevance_for(
                rerank_score=result.rerank_score,
                score=result.score,
                fusion=fusion,
                algorithm=algorithm,
                rerank_model=settings.search_rerank_model,
            )
            result_data: dict[str, Any] = {
                "id": doc_id,
                "doc_type": result.doc_type,
                "title": result.title,
                "score": result.score,
                # Always present, unlike `score` which is only interpretable if
                # you know which algorithm and fusion produced it. Read
                # `relevance_source` before rendering — only the calibrated
                # source may be shown as a percentage. See ADR-034.
                "relevance": relevance,
                "relevance_source": relevance_source,
            }
            # Additive, and only when reranking ran. `score` keeps the retrieval
            # value so `score_threshold` (applied against it inside Qdrant)
            # still refers to the same quantity a caller filters on.
            if result.rerank_score is not None:
                result_data["rerank_score"] = result.rerank_score

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
            "granularity": granularity,
            # The prevalence the relevance curves were fitted at (ADR-034).
            # Shipped WITH the number rather than left to documentation: a
            # corpus whose relevant-document rate differs from this biases
            # the value in a direction a caller can only reason about if it
            # knows the fit point. Ordering is unaffected either way.
            "relevance_fit_base_rate": relevance_fit_base_rate(RELEVANCE_ORDINAL),
            # False both when not requested and when requested-but-degraded, so
            # a caller can always tell which ordering it received.
            "reranked": rerank_outcome == RERANK_APPLIED,
        }

        # Optional PCA coordinates. PCA plots the result chunks around the query's
        # dense embedding. Keyword-only result chunks (``keyword-index`` tag) carry
        # no dense vector, so compute_pca_coordinates places them at the origin
        # (they can't be positioned) — the hybrid chunks still plot normally.
        if include_pca and len(paginated_results) >= 2:
            try:
                if search_algo.query_embedding is not None:
                    query_embedding = search_algo.query_embedding
                else:
                    provider = get_provider()
                    query_embedding = await provider.embed(query)

                pca_data = await compute_pca_coordinates(
                    paginated_results, query_embedding
                )
                response_data["pca_data"] = pca_data
            except Exception as e:
                logger.warning("Failed to compute PCA for unified search: %s", e)

        reranked_label = _reranked_label(rerank, rerank_outcome)

        record_search_request(
            surface="http",
            algorithm=_search_algorithm_label(algorithm, fusion),
            granularity=granularity,
            reranked=reranked_label,
            status="success",
            results_returned=len(formatted_results),
            verification_dropped=dropped_count,
        )
        # Usage metering parity with nc_semantic_search. This endpoint embeds a
        # query — a real, billable provider cost — and until now recorded no
        # usage event at all, so Astrolabe-driven search (most traffic) was
        # invisible to the ledger while MCP-driven search was billed. Recording
        # it here makes the two entrypoints consistent; expect a step change in
        # tokens_embedded rather than a new charge.
        await record_search_usage(
            enabled=settings.usage_metering_enabled,
            user_id=user_id,
            fusion=fusion,
            doc_types=doc_types if isinstance(doc_types, list) else None,
            token_count=search_algo.query_token_count,
            surface="http",
        )

        return JSONResponse(response_data)

    except Exception as e:
        # exception() over error(): keeps the traceback and satisfies Sonar
        # python:S8572 (logging.error with the exception object in an except).
        logger.exception("Error in unified search")
        record_search_request(
            surface="http",
            algorithm=_search_algorithm_label(algorithm, fusion),
            granularity=granularity,
            reranked="false",
            status="error",
        )
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
        "doc_types": ["note", "file"],  // optional filter by document types
        "rerank": false  // opt-in cross-encoder rerank; 422 unless the server
                         // advertises rerank_available on /api/v1/status
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

    # Bound before the try so the error-path metric can label the request even
    # when parsing is what failed — same reason as unified_search.
    algorithm = "unknown"
    fusion = "rrf"

    try:
        # Parse request body
        body = await request.json()
        query = body.get("query", "")
        requested_algorithm = body.get("algorithm")  # None ⇒ graceful default
        fusion = body.get("fusion", "rrf")
        score_threshold = body.get("score_threshold", 0.0)
        limit = min(body.get("limit", 10), 50)  # Enforce max limit
        # Validated rather than read raw: this is a new parameter, so it starts
        # with the same checking /api/v1/search applies. (The pre-existing
        # `score_threshold`/`limit` reads above still lack it — tracked
        # separately rather than widened here.)
        try:
            min_relevance = _parse_float_param(
                body.get("min_relevance"), 0.0, 0.0, 1.0, "min_relevance"
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        include_pca = body.get("include_pca", True)
        doc_types = body.get("doc_types")  # Optional list of document types
        # Optional cross-encoder rerank, same flag and same gating as
        # /api/v1/search — including the split between the shape check here and
        # the capability gate after the algorithm build, so the two endpoints
        # order their validations identically. This surface is the one
        # Astrolabe's search page calls, so without it the reranked ordering —
        # and any signal derived from it — is unreachable from the UI no matter
        # what the server can serve.
        rerank, rerank_error = _parse_rerank(body)
        if rerank_error is not None:
            return rerank_error
        # ADR-027 Phase 2 path filter (files only); blank ⇒ no filter. Accept a
        # path_prefixes list (multi-folder) alongside the legacy single
        # path_prefix; normalize drops blanks and de-dupes.
        # path_prefixes arrives as a JSON array (the Astrolabe PHP client sends
        # a list); any other shape is ignored rather than guessed at. The legacy
        # single path_prefix is folded in by normalize_path_prefixes.
        _path_prefixes_raw = body.get("path_prefixes")
        path_prefixes = normalize_path_prefixes(
            body.get("path_prefix"),
            _path_prefixes_raw if isinstance(_path_prefixes_raw, list) else None,
        )
        # ADR-027 modified-date range filter. Accepts RFC 3339 / ISO 8601
        # datetimes or Unix seconds; normalized to int Unix seconds. None ⇒ open.
        try:
            modified_after = parse_modified_timestamp(
                body.get("modified_after"), param_name="modified_after"
            )
            modified_before = parse_modified_timestamp(
                body.get("modified_before"), param_name="modified_before"
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        if not query:
            return JSONResponse(
                {"error": "Missing required parameter: query"},
                status_code=400,
            )

        if (
            modified_after is not None
            and modified_before is not None
            and modified_after > modified_before
        ):
            return JSONResponse(
                {"error": "modified_after must be <= modified_before"},
                status_code=400,
            )

        # Resolve + build the search algorithm: an *explicit* unsupported request
        # (e.g. any algorithm while vector sync is disabled) is rejected with 422
        # carrying the advertised supported_search_types, so the client can
        # correct it rather than silently receive fallback results. An absent
        # algorithm still defaults gracefully.
        try:
            search_algo, algorithm, fusion = _build_search_algorithm(
                requested_algorithm,
                settings,
                score_threshold=score_threshold,
                fusion=fusion,
            )
        except UnsupportedSearchType as e:
            return _unsupported_search_type_response(e)

        # Capability gate after the query and algorithm checks, matching
        # /api/v1/search so an unsupported algorithm still wins over an
        # unconfigured reranker on both endpoints.
        rerank_error = _rerank_capability_error(rerank, settings)
        if rerank_error is not None:
            return rerank_error

        rerank_outcome = RERANK_SKIPPED
        # Reranking can only reorder what retrieval supplied, so it needs a
        # deeper candidate pool than the caller's limit. This endpoint always
        # searches chunks (grouped=False) and has no offset, so the floor is
        # simply `limit` — which is also what it retrieves when rerank is off,
        # leaving that path byte-identical to before. NB with several
        # `doc_types` this depth is fetched PER TYPE before the merge, so
        # retrieval cost scales with len(doc_types) — the same shape as
        # unified_search's own doc_types loop.
        retrieval_limit = (
            effective_pool_size(settings, floor=limit, grouped=False)
            if rerank
            else limit
        )

        async def _execute(scope: AccessibleScope | None) -> list:
            """Run the search across requested doc_types with the given access
            scope (None ⇒ self-only)."""
            owners = scope.owners if scope else None
            # Narrow the owner branch to the subtrees actually shared with the
            # caller; see access_filter.build_ownership_filter.
            roots = scope.share_root_ids if scope else None
            results: list = []
            if doc_types and isinstance(doc_types, list):
                # Search each doc_type separately and merge results
                for doc_type in doc_types:
                    if doc_type:  # Skip empty strings
                        results.extend(
                            await search_algo.search(
                                query=query,
                                user_id=user_id,
                                limit=retrieval_limit,
                                doc_type=doc_type,
                                accessible_owners=owners,
                                shared_root_ids=roots,
                                modified_after=modified_after,
                                modified_before=modified_before,
                                path_prefixes=path_prefixes,
                            )
                        )
                # Sort merged results by score and limit
                results.sort(key=lambda r: r.score, reverse=True)
                results = results[:retrieval_limit]
            else:
                # Search all document types
                results = await search_algo.search(
                    query=query,
                    user_id=user_id,
                    limit=retrieval_limit,
                    accessible_owners=owners,
                    shared_root_ids=roots,
                    modified_after=modified_after,
                    modified_before=modified_before,
                    path_prefixes=path_prefixes,
                )
            # Rerank inside the closure, i.e. BEFORE verify-on-read runs in
            # _search_with_acl, and after the per-doc_type merge so one pass
            # covers every type — same ordering as unified_search.
            nonlocal rerank_outcome
            if rerank:
                results, rerank_outcome = await rerank_results(
                    results, query, settings=settings, surface="http_viz"
                )
                results = sorted(results, key=_rerank_sort_key, reverse=True)
            # Relevance cut before the trim, so a filtered request still fills
            # up to `limit` with qualifying rows instead of returning whatever
            # is left of the top `limit`. Runs with or without reranking.
            results = filter_by_relevance(
                results,
                min_relevance=min_relevance,
                fusion=fusion,
                algorithm=algorithm,
                rerank_model=settings.search_rerank_model,
            )
            if rerank:
                # Cut the deep pool back BEFORE this returns into verify-on-read.
                # The pool exists for the reranker, not for verification, which
                # costs one Nextcloud round-trip per candidate — leaving 200 rows
                # here would turn enabling rerank into an order-of-magnitude load
                # increase for provisioned callers. This endpoint has no offset
                # or pagination, so the caller's `limit` IS the budget.
                #
                # It is also what keeps the response shape unchanged: all_results
                # feeds the PCA plot below, so an untrimmed pool would return 200
                # rows and 200 plotted points to a caller that asked for 10.
                results = results[:limit]
            return results

        all_results, dropped_count = await _search_with_acl(request, user_id, _execute)

        # Format results for PHP client
        formatted_results = []
        for result in all_results:
            relevance, relevance_source = relevance_for(
                rerank_score=result.rerank_score,
                score=result.score,
                fusion=fusion,
                algorithm=algorithm,
                rerank_model=settings.search_rerank_model,
            )
            formatted_result = {
                "id": result.id,
                "doc_type": result.doc_type,
                "title": result.title,
                "excerpt": result.excerpt[:200] if result.excerpt else "",
                "score": result.score,
                # The number a UI can filter and render honestly; `score` is a
                # rank artifact whose scale depends on the algorithm. Gate any
                # percentage rendering on `relevance_source`. See ADR-034.
                "relevance": relevance,
                "relevance_source": relevance_source,
                "metadata": result.metadata,
                # Chunk information for context display
                "chunk_index": result.chunk_index,
                "total_chunks": result.total_chunks,
            }
            # Additive, and only when reranking ran — same contract as
            # /api/v1/search. `score` keeps the retrieval value so
            # `score_threshold`, applied against it inside Qdrant, still refers
            # to the same quantity a caller filters on.
            if result.rerank_score is not None:
                formatted_result["rerank_score"] = result.rerank_score
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
            # See the sibling endpoint: the fit prevalence ships with the value.
            "relevance_fit_base_rate": relevance_fit_base_rate(RELEVANCE_ORDINAL),
            # False both when not requested and when requested-but-degraded, so
            # a caller can always tell which ordering it received.
            "reranked": rerank_outcome == RERANK_APPLIED,
        }

        # Compute PCA coordinates for visualization using shared function. PCA
        # plots chunks around the query's dense embedding; keyword-only chunks
        # (``keyword-index`` tag) have no dense vector and are placed at the origin
        # by compute_pca_coordinates while hybrid chunks plot normally.
        if include_pca and len(all_results) >= 2:
            try:
                # Get query embedding from search algorithm or generate it
                if search_algo.query_embedding is not None:
                    query_embedding = search_algo.query_embedding
                else:
                    provider = get_provider()
                    query_embedding = await provider.embed(query)

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

        # The third search entrypoint, and it embeds a query exactly like the
        # other two — so omitting metering here would recreate the very ledger
        # blind spot this change closes on /api/v1/search. Its own surface label
        # keeps the visualization route from being conflated with the search
        # route in dashboards.
        record_search_request(
            surface="http_viz",
            algorithm=_search_algorithm_label(algorithm, fusion),
            granularity=GRANULARITY_CHUNK,
            reranked=_reranked_label(rerank, rerank_outcome),
            status="success",
            results_returned=len(formatted_results),
            verification_dropped=dropped_count,
        )
        await record_search_usage(
            enabled=settings.usage_metering_enabled,
            user_id=user_id,
            fusion=fusion,
            doc_types=doc_types if isinstance(doc_types, list) else None,
            token_count=search_algo.query_token_count,
            surface="http_viz",
        )

        return JSONResponse(response_data)

    except Exception as e:
        # The client only ever sees a sanitized message, so without this the
        # traceback is lost entirely and a 500 leaves no trace anywhere.
        logger.exception("Error in vector search")
        # Without this sample the visualization surface drops out of "error rate
        # by surface" entirely — it would report successes and nothing else,
        # which reads as a perfectly healthy endpoint no matter how often it
        # fails.
        record_search_request(
            surface="http_viz",
            algorithm=_search_algorithm_label(algorithm, fusion),
            granularity=GRANULARITY_CHUNK,
            reranked="false",
            status="error",
        )
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

        # Validate required parameters. Written as a chained `and` rather than
        # `all([...])` + four `assert`s: `all()` does not narrow the types for
        # the checker, and the asserts that stood in for it sat inside this
        # try/except Exception, which would have turned a narrowing slip into a
        # 500 with an AssertionError body (python:S5779).
        if not (doc_type and doc_id and start_str and end_str):
            return JSONResponse(
                {
                    "success": False,
                    "error": "Missing required parameters: doc_type, doc_id, start, end",
                },
                status_code=400,
            )

        # Validate doc_id at the handler boundary: a malformed doc_id would
        # otherwise pass through to get_chunk_with_context and bottom out as a
        # 404 from deep inside, not a clear 400. Nextcloud IDs are unsigned
        # ints from MySQL auto_increment; doc_id stays a str downstream
        # (Qdrant payload index is keyword-typed). is_valid_nextcloud_doc_id
        # rejects "0", leading zeros, and Unicode digits that pass isdigit().
        #
        # Canonical TODO (referenced by
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
            raise ValueError(_NEXTCLOUD_HOST_NOT_CONFIGURED)

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
            # Expand to owners who shared content with the caller so the cached
            # chunk lookup can resolve cross-user SHARED FILES (gated per-file
            # inside get_chunk_with_context). Same expansion as the search path.
            accessible_owners = await list_accessible_owners(nc_client.sharing, user_id)
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
                accessible_owners=accessible_owners,
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
            # Reaching here means the file chunk context resolved, so access was
            # already confirmed (get_chunk_with_context gates files by id);
            # the bbox/page lookup uses the same owner scope for cross-user files.
            qdrant_bbox, qdrant_page = await get_chunk_bbox_and_page_from_qdrant(
                user_id=user_id,
                doc_id=doc_id,
                chunk_index=chunk_index,
                chunk_start=start,
                chunk_end=end,
                accessible_owners=accessible_owners,
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
        # Chunk-context 500s were previously invisible: the handler logged
        # nothing, so a failing chunk view left no server-side evidence at all.
        logger.exception("Error fetching chunk context")
        error_msg = _sanitize_error_for_client(e, "get_chunk_context")
        return JSONResponse(
            {"error": error_msg},
            status_code=500,
        )
