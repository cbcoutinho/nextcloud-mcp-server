"""Semantic search MCP tools using vector database."""

import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal

import anyio
from httpx import RequestError
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import (
    ToolAnnotations,
)
from pydantic import Field

from nextcloud_mcp_server.astrolabe_links import astrolabe_browser_base, chunk_url
from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.capabilities import allowed_doc_types
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.links import file_url
from nextcloud_mcp_server.models.semantic import (
    SemanticSearchResponse,
    SemanticSearchResult,
    VectorSyncStatusResponse,
)
from nextcloud_mcp_server.observability.metrics import (
    instrument_tool,
    record_search_request,
    record_search_stage,
)
from nextcloud_mcp_server.search.access_filter import (
    MAX_PATH_PREFIXES,
    list_accessible_scope,
    normalize_path_prefixes,
    resolve_prefix_folder_ids,
)
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.bm25_hybrid import (
    GRANULARITY_DOCUMENT,
    BM25HybridSearchAlgorithm,
    search_method_label,
)
from nextcloud_mcp_server.search.context import get_chunk_with_context
from nextcloud_mcp_server.search.relevance import (
    filter_by_relevance,
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
from nextcloud_mcp_server.utils.validation import parse_modified_timestamp
from nextcloud_mcp_server.vector.metrics_publisher import (
    count_indexed,
    estimate_hybrid_vector_bytes,
)
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

if TYPE_CHECKING:  # pragma: no cover - import cycle / lazy-import guard
    from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)


def _consent_narrowed_doc_types(
    doc_types: list[str] | None, allowed: frozenset[str]
) -> list[str]:
    """Apply the admin allow-set to a requested ``doc_types`` filter.

    Caller has already established ``allowed is not None`` (a concrete allow-set;
    ``None`` means "no restriction" and is handled by skipping this call). When
    no explicit ``doc_types`` are requested, restrict to the full allow-set
    (returned ``sorted`` for determinism only — order is a filter, not a ranking
    hint); otherwise intersect (preserving the caller's order). An empty result
    means nothing the caller asked for is admin-approved — the caller
    short-circuits to an empty response rather than falling through to an
    all-types search.
    """
    if doc_types is None:
        return sorted(allowed)
    return [dt for dt in doc_types if dt in allowed]


def _parse_modified_bounds(
    modified_after: str | int | None, modified_before: str | int | None
) -> tuple[int | None, int | None]:
    """Normalise the ADR-027 date bounds to int Unix seconds.

    The numeric ``modified_at`` Range filter needs seconds, and a bad format
    must surface as a clean MCPError rather than a 500.

    The ordering check lives here rather than on the signature because a
    per-parameter pydantic ``Field`` constraint bounds each date on its own but
    cannot express the relationship between them; unguarded, an inverted range
    silently returns zero results.
    """
    try:
        after_ts = parse_modified_timestamp(modified_after, param_name="modified_after")
        before_ts = parse_modified_timestamp(
            modified_before, param_name="modified_before"
        )
    except ValueError as exc:
        raise MCPError(code=-1, message=str(exc)) from exc

    if after_ts is not None and before_ts is not None and after_ts > before_ts:
        raise MCPError(
            code=-1,
            message=(
                "modified_after must be <= modified_before "
                f"(got modified_after={modified_after!r}, "
                f"modified_before={modified_before!r})"
            ),
        )
    return after_ts, before_ts


async def _retrieve_candidates(
    search_algo: BM25HybridSearchAlgorithm,
    *,
    doc_types: list[str] | None,
    fetch_limit: int,
    **search_kwargs: Any,
) -> list[SearchResult]:
    """The unverified candidate pool for one search.

    ``doc_types=None`` is a single cross-app query. A list issues ONE query per
    type, so the pre-merge pool is N x ``fetch_limit`` rather than
    ``fetch_limit`` -- more Qdrant work than the cross-app branch. The trim
    below clamps it back so verification (and the Nextcloud round-trips it
    triggers) sees the same budget either way; the per-type Qdrant cost stays
    higher and scales with ``len(doc_types)``.

    Extracted from ``nc_semantic_search`` for nesting rather than reuse.
    """
    if doc_types is None:
        # ADR-019: the caller over-fetches to absorb ghost-record drops during
        # verify-on-read; the trim to the requested ``limit`` happens after
        # verification, not here. When ghost density is high (a large board
        # share was just revoked, say) that budget can still under-deliver
        # against the requested limit; the index self-heals via lazy eviction,
        # so subsequent searches recover. The 2x factor is a deliberate v1
        # trade-off — raising it costs Nextcloud round-trips on every search.
        # TODO(ADR-019): expose VERIFICATION_OVERFETCH so operators with
        # persistent high ghost density can tune this without a code change.
        return list(await search_algo.search(doc_type=None, **search_kwargs))

    all_results: list[SearchResult] = []
    for dtype in doc_types:
        all_results.extend(await search_algo.search(doc_type=dtype, **search_kwargs))

    # Sort then cap to match the cross-app branch's over-fetch budget. Without
    # this, N doc_types x fetch_limit results would all flow into verification,
    # multiplying the Nextcloud round-trip cost by N.
    all_results.sort(key=lambda r: r.score, reverse=True)
    return all_results[:fetch_limit]


def _fetch_limit(
    settings: Any, *, overfetch: int, rerank: bool, granularity: str
) -> int:
    """How many candidates to retrieve before verification.

    Reranking can only reorder what retrieval supplied, so it needs a deeper
    pool than the verification over-fetch — never smaller than that over-fetch,
    and clamped for grouped search (see ``effective_pool_size``). Without
    reranking the over-fetch is the whole budget.
    """
    if not rerank:
        return overfetch
    return effective_pool_size(
        settings, floor=overfetch, grouped=granularity == GRANULARITY_DOCUMENT
    )


async def _rerank_pool(
    all_results: list[SearchResult],
    query: str,
    *,
    settings: Any,
    verification_budget: int,
) -> tuple[list[SearchResult], str, str]:
    """Rerank the merged candidate pool and report how it went.

    Returns ``(results, outcome, metric_label)``. SKIPPED — nothing to reorder —
    reports as ``"false"`` rather than ``"unavailable"``, otherwise every query
    returning 0-1 rows would register as a reranker outage.

    The trim back to ``verification_budget`` keeps verify-on-read at the same
    number of Nextcloud round-trips as an unreranked search: the deep pool
    exists for the reranker, not for verification.
    """
    all_results, outcome = await rerank_results(
        all_results, query, settings=settings, surface="mcp"
    )
    metric_reranked = {
        RERANK_APPLIED: "true",
        RERANK_DEGRADED: "unavailable",
    }.get(outcome, "false")
    return all_results[:verification_budget], outcome, metric_reranked


def _log_top_results(verified_results: list[SearchResult]) -> None:
    """Log the top few results, titles included.

    Safe only *after* verify-on-read: the caller is confirmed to have access to
    these. Unverified titles are never logged — see the search algorithms.
    """
    if not verified_results:
        return
    logger.debug(
        "Top verified results: %s",
        ", ".join(
            f"{r.doc_type}_{r.id} (score={r.score:.3f}, title='{r.title}')"
            for r in verified_results[:5]
        ),
    )


def _to_semantic_results(
    search_results: list[SearchResult],
    *,
    browser_base: str | None,
    fusion: str,
    rerank_model: str | None,
) -> list[SemanticSearchResult]:
    """Map retrieved rows onto the public response model.

    Extracted from ``nc_semantic_search`` for nesting rather than reuse: the
    ``file_url`` ternary sat five levels deep inside that function.
    """
    # Convert SearchResult objects to SemanticSearchResult for response.
    # SearchResult.id is `str` (Qdrant keyword-indexed payload), but
    # every currently indexed type uses numeric ids and the MCP response
    # model narrows to `int`. Casting here makes the narrowing explicit
    # and surfaces any future non-numeric-id type as a loud failure at
    # the boundary instead of silently widening the public API.
    results = []
    for r in search_results:
        try:
            narrowed_id = int(r.id)
        except (TypeError, ValueError) as e:
            # Re-raise with explicit context so the outer handler logs
            # something operators can act on (the generic "Search
            # failed: invalid literal for int()" is opaque).
            raise TypeError(
                f"SemanticSearchResult.id must be int-convertible, "
                f"got {r.id!r} (type={type(r.id).__name__}) for "
                f"doc_type={r.doc_type!r}. This indicates a doc_type "
                f"with non-numeric ids has been indexed but the "
                f"public response model has not been widened. Add "
                f"the doc_type to the SemanticSearchResult.id type "
                f"or convert at the verifier layer."
            ) from e
        relevance, relevance_source = relevance_for(
            rerank_score=r.rerank_score,
            score=r.score,
            fusion=fusion,
            # This tool always runs BM25HybridSearchAlgorithm, so the
            # fused-score branch is the right one; it never takes the
            # dense-only cosine path.
            algorithm="hybrid",
            rerank_model=rerank_model,
        )
        metadata = r.metadata or {}
        # board_id is the only one of Astrolabe's access-recheck
        # identifiers the chunk payload carries (see
        # build_search_result_from_point); the others fall through to
        # its MCP backstop. Tested against None rather than falsiness to
        # match chunk_url's handling of a legitimate 0 — Nextcloud ids
        # are 1-based, so this is consistency, not a live bug.
        board_id = metadata.get("board_id")
        link_extra = None if board_id is None else {"board_id": str(board_id)}
        results.append(
            SemanticSearchResult(
                id=narrowed_id,
                doc_type=r.doc_type,
                title=r.title,
                rerank_score=r.rerank_score,
                relevance=relevance,
                relevance_source=relevance_source,
                category=metadata.get("category", ""),
                excerpt=r.excerpt,
                score=r.score,
                chunk_index=metadata.get("chunk_index", 0),
                total_chunks=metadata.get("total_chunks", 1),
                chunk_start_offset=r.chunk_start_offset,
                chunk_end_offset=r.chunk_end_offset,
                page_number=r.page_number,
                page_end=r.page_end,
                url=chunk_url(
                    browser_base,
                    doc_type=r.doc_type,
                    doc_id=narrowed_id,
                    chunk_start=r.chunk_start_offset,
                    chunk_end=r.chunk_end_offset,
                    title=r.title,
                    path=metadata.get("path"),
                    page_number=r.page_number,
                    chunk_index=metadata.get("chunk_index"),
                    total_chunks=metadata.get("total_chunks"),
                    extra=link_extra,
                ),
                # For doc_type="file" the doc_id IS the Nextcloud
                # fileid (vector/scanner.py indexes it as such), so the
                # /f/ link needs no extra lookup. Guarded on doc_type
                # because no other indexed type's id is a fileid — a
                # note id fed to /f/ would open an unrelated file or
                # 404.
                file_url=(
                    file_url(browser_base, narrowed_id)
                    if r.doc_type == "file"
                    else None
                ),
            )
        )
    return results


async def _expand_results_with_context(
    results: list[SemanticSearchResult],
    *,
    include_context: bool,
    client: "NextcloudClient",
    username: str,
    context_chars: int,
    accessible_owners: list[str],
) -> list[SemanticSearchResult]:
    """Return ``results`` with surrounding document context filled in.

    A no-op unless ``include_context`` is set, or when there is nothing to
    expand — the guard lives here so the caller reads as a single step.

    Extracted from ``nc_semantic_search`` because it carried most of that
    function's nesting, not because it is reused. Failure here is never fatal:
    a result whose context cannot be fetched is returned unchanged, so context
    expansion degrades to the plain excerpt rather than failing the search.
    """
    if not include_context or not results:
        return results

    logger.info(
        "Expanding %d results with context (context_chars=%d)",
        len(results),
        context_chars,
    )

    # Fetch context for all results in parallel.
    # Limit concurrent requests to prevent connection pool exhaustion.
    #
    # Intentionally distinct from settings.verification_concurrency:
    # that knob bounds Nextcloud round-trips during access
    # verification (ADR-019); this one bounds context-expansion
    # fetches that run only when ``include_context=True``. Operators
    # tuning one rarely want the other in lockstep, so they share
    # the default value (20) but not the env var.
    max_concurrent = 20
    semaphore = anyio.Semaphore(max_concurrent)
    expanded_results = [None] * len(results)

    async def fetch_context(index: int, result: SemanticSearchResult):
        """Fetch context for a single result (parallel with semaphore)."""
        async with semaphore:
            # Only expand if we have valid chunk offsets
            if result.chunk_start_offset is None or result.chunk_end_offset is None:
                # Keep result as-is without context expansion
                expanded_results[index] = result
                return

            try:
                chunk_context = await get_chunk_with_context(
                    nc_client=client,
                    user_id=username,
                    # SemanticSearchResult.id is the int-narrowed
                    # public form; get_chunk_with_context queries
                    # Qdrant where doc_id is keyword-indexed as str.
                    doc_id=str(result.id),
                    doc_type=result.doc_type,
                    chunk_start=result.chunk_start_offset,
                    chunk_end=result.chunk_end_offset,
                    page_number=result.page_number,
                    chunk_index=result.chunk_index,
                    total_chunks=result.total_chunks,
                    context_chars=context_chars,
                    # Forward the share-expanded owner set so context
                    # expansion works for shared files (the per-file
                    # file_accessible_by_id gate inside still enforces
                    # access). Without this the lookup stays self-only
                    # and silently falls back to the plain excerpt.
                    accessible_owners=accessible_owners,
                )

                if chunk_context:
                    # Create new result with context fields populated
                    expanded_results[index] = SemanticSearchResult(
                        id=result.id,
                        doc_type=result.doc_type,
                        title=result.title,
                        category=result.category,
                        excerpt=result.excerpt,
                        score=result.score,
                        # This site REBUILDS the row rather than
                        # copying it, so any field omitted here
                        # silently reverts to its default. Dropping
                        # this one produced a response reporting
                        # reranked=true whose every row carried
                        # rerank_score=null. See
                        # test_semantic_result_field_parity.py.
                        rerank_score=result.rerank_score,
                        relevance=result.relevance,
                        relevance_source=result.relevance_source,
                        chunk_index=result.chunk_index,
                        total_chunks=result.total_chunks,
                        chunk_start_offset=result.chunk_start_offset,
                        chunk_end_offset=result.chunk_end_offset,
                        page_number=result.page_number,
                        page_end=result.page_end,
                        url=result.url,
                        file_url=result.file_url,
                        # Context expansion fields
                        has_context_expansion=True,
                        marked_text=chunk_context.marked_text,
                        before_context=chunk_context.before_context,
                        after_context=chunk_context.after_context,
                        has_before_truncation=chunk_context.has_before_truncation,
                        has_after_truncation=chunk_context.has_after_truncation,
                    )
                    logger.debug(
                        "Expanded context for %s %s",
                        result.doc_type,
                        result.id,
                    )
                else:
                    # Context expansion failed, keep original result
                    expanded_results[index] = result
                    logger.debug(
                        "Failed to expand context for %s %s, keeping original result",
                        result.doc_type,
                        result.id,
                    )
            except Exception as e:
                # Context expansion failed, keep original result
                expanded_results[index] = result
                logger.warning(
                    "Error expanding context for %s %s: %s",
                    result.doc_type,
                    result.id,
                    e,
                )

    # Run all context fetches in parallel using anyio task group
    async with anyio.create_task_group() as tg:
        for idx, result in enumerate(results):
            tg.start_soon(fetch_context, idx, result)

    # Replace results with expanded versions
    results = [r for r in expanded_results if r is not None]
    logger.info(
        "Context expansion completed: %d results with context",
        len(results),
    )
    return results


@require_scopes("semantic.read")
@instrument_tool
async def nc_semantic_search(  # NOSONAR(S107)
    # S107 (too many parameters) is suppressed deliberately: for an MCP tool
    # the parameter list IS the wire schema that MCPServer publishes to
    # clients. Grouping these into a settings object to satisfy the rule
    # would change the tool's advertised interface and break every caller,
    # so the smell is inherent to the surface rather than to this function.
    query: str,
    ctx: Context,
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
    doc_types: list[str] | None = None,
    score_threshold: Annotated[float, Field(ge=0.0)] = 0.0,
    min_relevance: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description=(
                "Drop results whose `relevance` falls below this. Unlike "
                "`score_threshold` — which Qdrant applies to the raw "
                "retrieval score BEFORE deduplication, reranking and "
                "access verification, and which is therefore a recall cut "
                "that can remove the row reranking would have promoted to "
                "the top — this filters at the end, on the same [0,1] "
                "number reported on each result. Use it to ask for 'only "
                "results at least this relevant'. 0.0 (default) filters "
                "nothing. Note that relevance is relative to what was "
                "retrieved: search cannot abstain, so raising this is how "
                "you get an empty answer for a query the corpus has no "
                "answer to."
            ),
        ),
    ] = 0.0,
    fusion: str = "rrf",
    granularity: Annotated[
        Literal["chunk", "document"],
        Field(
            description=(
                "Result granularity. 'chunk' (default) returns the "
                "best-matching passages, so a long document can occupy "
                "several result slots. 'document' returns one row per "
                "document (its best-matching chunk), which is the right "
                "shape for 'which files mention X' / 'list documents "
                "about Y' — `limit` then counts documents rather than "
                "passages. Use 'chunk' when you want the passage text to "
                "answer from, 'document' when you want to enumerate "
                "sources. Note 'document' improves result diversity, not "
                "recall: a document whose best chunk ranks too low to be "
                "retrieved is absent under both settings. Caveat when "
                "combined with the default doc_types=None (search all "
                "types): grouping keys on the numeric document id, which "
                "is not unique across types, so a note and a file that "
                "happen to share an id can be merged into one result. "
                "Pass an explicit doc_types (e.g. ['file']) to avoid this."
            ),
        ),
    ] = "chunk",
    rerank: Annotated[
        bool,
        Field(
            description=(
                "Re-score the retrieved candidates with a cross-encoder "
                "before returning them. This is the single largest "
                "available improvement to result ordering — it reads each "
                "candidate against the query directly, rather than relying "
                "on the rank fusion that retrieval produced — and it is "
                "most worth using for questions where the right answer may "
                "not be the closest lexical or semantic match. It costs "
                "roughly a second of extra latency, so prefer it for "
                "precision-sensitive questions over quick lookups. "
                "Requires the server to have reranking configured; asking "
                "for it on a server without it is an error rather than a "
                "silent downgrade. The response's `reranked` field reports "
                "whether it actually applied — reranking never fails a "
                "search, so a reranker outage degrades to retrieval order "
                "with `reranked=false`."
            ),
        ),
    ] = False,
    include_context: bool = False,
    context_chars: Annotated[int, Field(ge=0)] = 300,
    modified_after: Annotated[
        str | int | None,
        Field(
            description=(
                "Only return documents modified at or after this time. "
                "RFC 3339 / ISO 8601 datetime (e.g. '2026-01-01T00:00:00Z') "
                "or Unix seconds. None = no lower bound."
            ),
        ),
    ] = None,
    modified_before: Annotated[
        str | int | None,
        Field(
            description=(
                "Only return documents modified at or before this time. "
                "RFC 3339 / ISO 8601 datetime or Unix seconds. "
                "None = no upper bound."
            ),
        ),
    ] = None,
    path_prefix: Annotated[
        str | None,
        Field(
            description=(
                "Deprecated single-folder filter; prefer path_prefixes. "
                "Restrict to files under this folder/path "
                "(e.g. '/Projects/Reports'). Matches the file_path of "
                "indexed files only, so setting it implicitly limits "
                "results to files. None = no path filter."
            ),
        ),
    ] = None,
    path_prefixes: Annotated[
        list[str] | None,
        Field(
            max_length=MAX_PATH_PREFIXES,
            description=(
                "Restrict to files under any of these folders/paths "
                "(e.g. ['/Projects/Reports', '/Shared/Specs']). Folders are "
                "OR-ed together. Matches the file_path of indexed files "
                "only, so setting it implicitly limits results to files. "
                f"Capped at {MAX_PATH_PREFIXES} folders to bound the "
                "OR-filter width. None or empty = no path filter."
            ),
        ),
    ] = None,
) -> SemanticSearchResponse:
    """
    Search Nextcloud content across apps, indexed in Qdrant.

    Qdrant native hybrid search combining dense semantic vectors (conceptual
    similarity, natural language) and BM25 sparse vectors (precise
    keyword/acronym matching), fused in the database for optimal relevance.
    Documents indexed keyword-only (``keyword-index`` tag) carry no dense
    vector and so contribute via the BM25 sparse side only. They appear in the
    same unified result set.

    Requires VECTOR_SYNC_ENABLED=true. Supports indexing of notes, files,
    news items, deck cards, and mail messages.

    Args:
        query: Natural language or keyword search query
        limit: Maximum number of results to return (default: 10)
        doc_types: Document types to search (e.g., ["note", "file", "deck_card", "news_item", "mail_message"]). None = search all indexed types (default)
        score_threshold: Minimum fusion score. NOT a relevance percentage —
            the fused score is a rank artifact, not a calibrated measure.
            With RRF the top result scores about 2/VECTOR_SEARCH_RRF_K
            (~0.033 at the default k=60), so a "10% relevant" reading of
            0.1 returns nothing at all. Leave at 0.0 (the default) and cut
            by rank via `limit` instead. DBSF scores are distribution-
            normalized and can exceed 1.0.
            Two further reasons not to drive a UI control from this. It is
            only meaningful for dense-only search, where the score is a
            cosine similarity genuinely in [0, 1]. And it is applied by
            Qdrant on the fused score BEFORE deduplication, reranking and
            verify-on-read, so it is a recall cut taken before reranking can
            reorder anything — a threshold that merely looks conservative
            can silently remove the result the reranker would have promoted
            to the top.
        fusion: Fusion algorithm: "rrf" (Reciprocal Rank Fusion, default) or "dbsf" (Distribution-Based Score Fusion)
               RRF: Good general-purpose fusion using reciprocal ranks
               DBSF: Uses distribution-based normalization, may better balance different score ranges
        include_context: Whether to expand results with surrounding context (default: False)
        context_chars: Number of characters to include before/after matched chunk (default: 300)
        modified_after: Only return documents whose last-modified time is at or after this
            instant. Accepts an RFC 3339 / ISO 8601 datetime (e.g. "2026-01-01T00:00:00Z",
            a naive datetime is treated as UTC) or Unix seconds. None = no lower bound
            (default).
        modified_before: Only return documents whose last-modified time is at or before this
            instant. Same formats as modified_after. None = no upper bound (default). Must be
            >= modified_after when both are supplied.
        path_prefix: Deprecated single-folder filter. Prefer path_prefixes. Restrict to files
            under this folder/path (e.g. "/Projects/Reports"). Folded into path_prefixes.
        path_prefixes: Restrict to files under any of these folders/paths (OR-ed), e.g.
            ["/Projects/Reports", "/Shared/Specs"]. Matches the file_path of indexed files
            only — setting it implicitly limits results to files. None/empty = no path filter
            (default).

    To scope a search to one or more folders, pass `path_prefixes` — that is
    the supported way to search "just this subdirectory".

    Returns:
        SemanticSearchResponse with matching documents ranked by fusion scores.

        Each result carries a `url` that opens that exact chunk in the
        Astrolabe UI. Offer it alongside anything you quote from `excerpt`
        so the user can read the passage in place. It is None when the
        server has no browser-reachable Nextcloud base URL configured.

        Verification fields (ADR-019 verify-on-read):
        - verified_chunk_count: chunk rows that passed access checks
          (sized in chunks, counted before trimming to ``limit``, so it
          can exceed ``len(results)`` when a doc has multiple matching
          chunks).
        - dropped_document_count: unique ``(doc_id, doc_type)`` pairs
          evicted as ghost records during this search (sized in
          documents, not chunks).
    """
    settings = get_settings()
    client = await get_client(ctx)
    username = client.username

    # Self-describing method label, mirroring BM25HybridSearchAlgorithm: the
    # query always fuses dense + sparse prefetches (keyword-only documents
    # contribute via the sparse side), so the label is always the fusion one.
    # Derived from the BOUNDED label helper, not by interpolating the raw
    # parameter. `fusion` is caller-controlled and is not validated until
    # the algorithm is constructed inside the try below — but this value
    # becomes a Prometheus label on every exit path including the error one,
    # so an arbitrary string here would mint a permanent time series per
    # distinct value. An invalid mode still raises when the algorithm is
    # built; this only bounds what gets reported.
    search_method = search_method_label(fusion)

    logger.info(
        "%s: query=%r, user=%s, limit=%d, score_threshold=%s, fusion=%s",
        search_method,
        query,
        username,
        limit,
        score_threshold,
        fusion,
    )

    # Check that vector sync is enabled — search queries the Qdrant index.
    if not settings.vector_sync_enabled:
        raise MCPError(
            code=-1,
            message="Cross-app search requires VECTOR_SYNC_ENABLED=true",
        )

    modified_after_ts, modified_before_ts = _parse_modified_bounds(
        modified_after, modified_before
    )

    # Capability gate. Rejecting is deliberate: silently returning
    # unreranked results to a caller that explicitly asked for reranking
    # would be indistinguishable from a ranking bug. Servers advertise the
    # capability on GET /api/v1/status so callers can check rather than
    # probe. (A reranker that is configured but momentarily unavailable is
    # a different case — that degrades, and the response says so.)
    if rerank and not rerank_available(settings):
        raise MCPError(
            code=-1,
            message=(
                "Reranking is not configured on this server. Set "
                "SEARCH_RERANK_ENABLED=true (requires "
                "EMBEDDING_GATEWAY_URL), or omit rerank to use "
                "retrieval ordering."
            ),
        )

    # Merge the legacy single path_prefix and the path_prefixes list into one
    # cleaned list, dropping blank/whitespace entries so an empty UI field
    # doesn't filter out every result (ADR-027 Phase 2).
    folder_prefixes = normalize_path_prefixes(path_prefix, path_prefixes)

    # ADR-033 Phase 3: resolve each folder prefix to its canonical Nextcloud
    # fileid so the query can scope by folder_ancestors — a true left-anchored
    # containment that is correct for every reader of a shared folder (its
    # fileid is user-agnostic). Best-effort: unresolved prefixes fall back to
    # the file_path MatchText branch inside build_base_filter_conditions.
    folder_ids = (
        await resolve_prefix_folder_ids(client.webdav, path_prefixes=folder_prefixes)
        if folder_prefixes
        else []
    )

    # Expand the caller's identity to every owner whose content they
    # have read access to via Nextcloud shares. Lets a user find files
    # owners have shared with them without having to re-index those
    # files under their own user_id. ``share_root_ids`` scopes that
    # expansion to the shared subtrees, so one incoming share does not
    # admit the whole owner's corpus as candidates for verify-on-read to
    # reject (which silently shortened result pages).
    accessible_scope = await list_accessible_scope(client.sharing, username)
    accessible_owners = accessible_scope.owners
    shared_root_ids = accessible_scope.share_root_ids

    # Admin consent gate: restrict to source types the Astrolabe admin has
    # approved (and that are installed for this user). This mirrors
    # Astrolabe's own server-side enforcement but is independent because
    # this tool queries Qdrant directly. ``None`` = no restriction
    # (fail-open / Astrolabe predating this feature). An empty allow-set
    # means the admin disabled every source.
    #
    # Perf trade-off (accepted): when Astrolabe is present and the caller
    # passed no doc_types, narrowing turns ``None`` into a concrete list, so
    # the search takes the per-type query branch (N queries) instead of the
    # single cross-type query. N is the count of admin-approved types
    # (typically 1-4), so the overhead is small; left as-is rather than
    # adding a "search all approved in one query" fast path.
    allowed = await allowed_doc_types(client, username)
    if allowed is not None:
        doc_types = _consent_narrowed_doc_types(doc_types, allowed)
        if not doc_types:
            logger.info(
                "Semantic search short-circuited for user %s: no requested "
                "doc_type is admin-approved for semantic search",
                username,
            )
            # A short-circuit is a successful search that found nothing, not
            # an error — recording it keeps the zero-result distribution
            # honest about how often consent narrowing is the cause.
            record_search_request(
                surface="mcp",
                algorithm=search_method,
                granularity=granularity,
                reranked="false",
                status="success",
                results_returned=0,
            )
            return SemanticSearchResponse(
                results=[],
                query=query,
                total_found=0,
                search_method=search_method,
                granularity=granularity,
                verified_chunk_count=0,
                dropped_document_count=0,
            )

    # Search-metric state, recorded in the ``finally`` below so every exit —
    # success, MCPError, or an unexpected raise — lands exactly one
    # ``astrolabe_search_requests_total`` sample. Defaults describe the
    # failure case; the success path overwrites them.
    metric_status = "error"
    metric_results: int | None = None
    metric_dropped = 0
    metric_reranked = "false"

    try:
        # The nc_semantic_search tool deliberately uses BM25-hybrid (dense +
        # sparse with RRF/DBSF fusion) as the single tool-layer algorithm.
        # SemanticSearchAlgorithm is not dead code — it backs the dense-only
        # option that the API surface exposes explicitly
        # (api/visualization.py). Both algorithms take accessible_owners,
        # so ACL-aware search works on every surface.
        search_algo = BM25HybridSearchAlgorithm(
            score_threshold=score_threshold, fusion=fusion
        )

        overfetch = limit * 2
        fetch_limit = _fetch_limit(
            settings, overfetch=overfetch, rerank=rerank, granularity=granularity
        )

        retrieve_start = anyio.current_time()

        # Execute search across requested document types
        all_results = await _retrieve_candidates(
            search_algo,
            doc_types=doc_types,
            fetch_limit=fetch_limit,
            query=query,
            user_id=username,
            limit=fetch_limit,
            score_threshold=score_threshold,
            accessible_owners=accessible_owners,
            shared_root_ids=shared_root_ids,
            granularity=granularity,
            modified_after=modified_after_ts,
            modified_before=modified_before_ts,
            path_prefixes=folder_prefixes,
            path_prefix_folder_ids=folder_ids,
        )

        # Covers query embedding + Qdrant across every branch above, so the
        # per-doc_type loop's higher cost is visible rather than averaged
        # away against the single-query branch.
        record_search_stage("mcp", "retrieve", anyio.current_time() - retrieve_start)

        # Rerank the merged pool BEFORE verification, and before trimming to
        # the verification budget. After the merge so one cross-encoder pass
        # covers every doc_type — which also makes the merge meaningful,
        # since fused scores from separate per-type queries were computed
        # against different candidate populations and are not really
        # comparable, whereas one reranker's scores are.
        rerank_outcome = RERANK_SKIPPED
        if rerank:
            all_results, rerank_outcome, metric_reranked = await _rerank_pool(
                all_results, query, settings=settings, verification_budget=limit * 2
            )

        # ADR-019: Verify-on-read. The vector index is a recall layer;
        # Nextcloud is the source of truth for access. Filter out ghost
        # records (deleted/unshared docs not yet reconciled by webhooks)
        # BEFORE trimming to `limit`, so we don't lose accessible results
        # to the limit slot that ghosts would otherwise occupy. We also
        # run this BEFORE context expansion to avoid re-fetching docs that
        # are about to be dropped. Pass the lifespan-owned task group so
        # eviction of dropped points is fire-and-forget (does not block
        # the response).
        # Direct attribute access — both AppContext and OAuthAppContext
        # expose ``eviction_task_group`` as a @property (see app.py),
        # reading dynamically from the module-level VectorSyncState
        # singleton. A defensive ``getattr(..., None)`` here would mask
        # typos; if a future lifespan-context type forgets the property,
        # AttributeError surfaces during the first search rather than
        # silently degrading to inline eviction for the life of the
        # process.
        lifespan_ctx: Any = ctx.request_context.lifespan_context
        eviction_task_group = lifespan_ctx.eviction_task_group
        verification_start = anyio.current_time()
        verified_results, dropped_count = await verify_search_results(
            client,
            all_results,
            eviction_task_group=eviction_task_group,
        )
        record_search_stage("mcp", "verify", anyio.current_time() - verification_start)
        verified_chunk_count = len(verified_results)
        logger.debug(
            "Verification completed in %.2fs: kept %d chunk(s), dropped %d doc(s)",
            anyio.current_time() - verification_start,
            verified_chunk_count,
            dropped_count,
        )
        _log_top_results(verified_results)
        # Relevance cut before the trim to `limit`, so a filtered search
        # still fills the page with qualifying rows rather than returning
        # whatever survives out of the top `limit`. Distinct from
        # `score_threshold`, which Qdrant applies to the raw retrieval score
        # before reranking has had a chance to reorder anything.
        verified_results = filter_by_relevance(
            verified_results,
            min_relevance=min_relevance,
            fusion=fusion,
            algorithm="hybrid",
            rerank_model=settings.search_rerank_model,
        )
        search_results = verified_results[:limit]

        # Resolved once per search rather than once per result: it reads
        # config and logs a warning when the base URL is unusable, and doing
        # that per row would repeat the warning `limit` times.
        browser_base = astrolabe_browser_base()

        results = _to_semantic_results(
            search_results,
            browser_base=browser_base,
            fusion=fusion,
            rerank_model=settings.search_rerank_model,
        )
        # Expand results with surrounding context if requested
        results = await _expand_results_with_context(
            results,
            include_context=include_context,
            client=client,
            username=username,
            context_chars=context_chars,
            accessible_owners=accessible_owners,
        )

        logger.info("Returning %d results from %s", len(results), search_method)

        # Usage metering (Deck #67): record the query embedding's token
        # count as a billable 'tokens_embedded' event. query_token_count
        # is set by BM25HybridSearchAlgorithm during the search() above; the
        # doc_types loop reuses one search_algo instance for the same query
        # and the algorithm caches the dense embedding per query, so the
        # query is embedded — and metered — exactly once regardless of how
        # many doc_types were searched. See record_search_usage for the
        # metric/privacy details.
        #
        # NOTE (v1 billing gap): this fires only on a fully successful
        # search. If the query embed succeeded (provider billed the tokens,
        # and Prometheus recorded them via record_embedding_tokens) but a
        # later step (Qdrant/verify) raised, no tokens_embedded row is
        # written — the embed cost is real but absent from the billing
        # ledger. Acceptable for v1 (search failures are rare and the meter
        # is not billed today); revisit if billing accuracy needs it.
        await record_search_usage(
            enabled=settings.usage_metering_enabled,
            user_id=username,
            fusion=fusion,
            doc_types=doc_types,
            token_count=search_algo.query_token_count,
            surface="mcp",
        )

        metric_status = "success"
        metric_results = len(results)
        metric_dropped = dropped_count

        return SemanticSearchResponse(
            results=results,
            query=query,
            total_found=len(results),
            search_method=search_method,
            granularity=granularity,
            reranked=rerank_outcome == RERANK_APPLIED,
            rerank_model=(
                settings.search_rerank_model
                if rerank_outcome == RERANK_APPLIED
                else None
            ),
            verified_chunk_count=verified_chunk_count,
            dropped_document_count=dropped_count,
        )

    except ValueError as e:
        error_msg = str(e)
        if "No embedding provider configured" in error_msg:
            raise MCPError(
                code=-1,
                message="Embedding service not configured. Set OLLAMA_BASE_URL environment variable.",
            )
        raise MCPError(code=-1, message=f"Configuration error: {error_msg}")
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error during search: {str(e)}")
    except Exception as e:
        # Genuinely-unexpected bucket (after the ValueError / RequestError
        # cases above). We convert it to an MCPError so the reason survives:
        # NextcloudMCPServer.call_tool maps that back to ToolError, which the
        # SDK delivers as is_error=True content the model can read. Raised
        # bare, mcp 2.x would replace the message with "Error executing tool
        # <name>". Neither path logs a server-side traceback, so — like the
        # sampling catch-all below — keep the stack here for triage.
        logger.exception("Search error: %s", e)
        raise MCPError(code=-1, message=f"Search failed: {str(e)}")
    finally:
        # One sample per search, on every exit path. Paired with the
        # identical call in api/visualization.py so the MCP and HTTP
        # entrypoints are directly comparable on one dashboard.
        record_search_request(
            surface="mcp",
            algorithm=search_method,
            granularity=granularity,
            reranked=metric_reranked,
            status=metric_status,
            results_returned=metric_results,
            verification_dropped=metric_dropped,
        )


@require_scopes("semantic.read")
@instrument_tool
async def nc_get_vector_sync_status(ctx: Context) -> VectorSyncStatusResponse:
    """Get the current vector sync status.

    Returns information about the vector sync process, including:
    - Number of documents indexed in the vector database
    - Number of documents pending processing
    - Current sync status (idle, syncing, or disabled)

    This is useful for determining when vector indexing is complete
    after creating or updating content across all indexed apps.
    """

    # Check if vector sync is enabled (supports both old and new env var names)
    settings = get_settings()
    if not settings.vector_sync_enabled:
        return VectorSyncStatusResponse(
            indexed_count=0,
            pending_count=0,
            status="disabled",
            enabled=False,
        )

    try:
        # Get document receive stream from lifespan context. Direct
        # attribute access matches the eviction_task_group pattern at
        # ``nc_semantic_search`` (see comment there): both AppContext
        # and OAuthAppContext define ``document_receive_stream``, so a
        # missing attribute is a typo that should fail loudly. The
        # value itself can legitimately be ``None`` before sync starts,
        # which the check below handles.
        # Outstanding-work view depends on the queue backend (Deck #183):
        # memory → stream buffer depth; postgres → procrastinate job counts.
        # Direct attribute access matches the eviction_task_group pattern at
        # ``nc_semantic_search``: both AppContext and OAuthAppContext define
        # these, so a missing attribute is a typo that should fail loudly.
        from nextcloud_mcp_server.vector.ingest_status import (  # noqa: PLC0415
            get_ingest_pending,
        )

        lifespan_ctx: Any = ctx.request_context.lifespan_context
        pending = await get_ingest_pending(
            task_producer=lifespan_ctx.task_producer,
            document_receive_stream=lifespan_ctx.document_receive_stream,
            ingest_queue=settings.ingest_queue,
        )

        # Corpus size: distinct documents AND total chunks (placeholders
        # excluded). A single "indexed" figure is ambiguous because each
        # document fans out to ~N chunks.
        indexed_documents = 0
        indexed_chunks = 0
        hybrid_chunks = 0
        estimated_vector_bytes = 0
        try:
            qdrant_client = await get_qdrant_client()
            indexed_documents, indexed_chunks = await count_indexed(
                qdrant_client, settings.get_collection_name()
            )
            # Hybrid chunks (dense-bearing) drive the vector-RAM footprint;
            # keyword-index chunks are sparse-only and cost no dense RAM (#624).
            # Shared helper so this and the HTTP status route can't drift.
            (
                hybrid_chunks,
                estimated_vector_bytes,
            ) = await estimate_hybrid_vector_bytes(
                qdrant_client,
                settings.get_collection_name(),
                settings.vector_ram_hnsw_overhead_factor,
            )
        except Exception as e:
            logger.warning("Failed to query Qdrant for indexed counts: %s", e)
            # Continue with zeroed counts

        # Determine status
        status = "syncing" if pending.pending > 0 else "idle"

        return VectorSyncStatusResponse(
            indexed_documents=indexed_documents,
            indexed_chunks=indexed_chunks,
            indexed_count=indexed_chunks,  # deprecated alias
            pending_count=pending.pending,
            status=status,
            enabled=True,
            ingest_queue=settings.ingest_queue,
            job_counts=pending.job_counts,
            job_counts_by_queue=pending.job_counts_by_queue,
            hybrid_chunks=hybrid_chunks,
            estimated_vector_bytes=estimated_vector_bytes,
        )

    except Exception as e:
        logger.error("Error getting vector sync status: %s", e)
        raise MCPError(
            code=-1,
            message=f"Failed to retrieve vector sync status: {str(e)}",
        )


def configure_semantic_tools(mcp: MCPServer):
    """Configure semantic search tools for MCP server."""

    mcp.tool(
        title="Semantic Search",
        annotations=ToolAnnotations(
            read_only_hint=True,  # Search doesn't modify data
            open_world_hint=True,  # Queries external Nextcloud service
        ),
    )(nc_semantic_search)

    mcp.tool(
        title="Check Indexing Status",
        annotations=ToolAnnotations(
            read_only_hint=True,  # Only checks status
            open_world_hint=True,
        ),
    )(nc_get_vector_sync_status)
