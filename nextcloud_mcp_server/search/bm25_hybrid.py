"""BM25 hybrid search algorithm using Qdrant native RRF fusion."""

import logging
from collections.abc import Iterable
from typing import Any

from qdrant_client import models
from qdrant_client.models import Filter

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.observability.metrics import (
    record_embedding_tokens,
    record_qdrant_operation,
)
from nextcloud_mcp_server.observability.tracing import trace_operation
from nextcloud_mcp_server.providers import get_bm25_service, get_provider
from nextcloud_mcp_server.search.access_filter import build_base_filter_conditions
from nextcloud_mcp_server.search.algorithms import (
    SearchAlgorithm,
    SearchResult,
    build_search_result_from_point,
)
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)

# Result granularity (ADR-027 follow-up, Deck #83). "chunk" is the historical
# behaviour and stays the default everywhere; "document" collapses each document
# to its single best-scoring chunk via Qdrant's native grouping.
# The only fusion modes Qdrant is asked for. Exported so every surface derives
# its metric label from the SAME bounded set — a caller-supplied fusion string
# that reached a Prometheus label would mint a permanent time series per distinct
# value, which is a cardinality-explosion vector rather than a cosmetic issue.
VALID_FUSIONS = ("rrf", "dbsf")

GRANULARITY_CHUNK = "chunk"
GRANULARITY_DOCUMENT = "document"
VALID_GRANULARITIES = (GRANULARITY_CHUNK, GRANULARITY_DOCUMENT)

# Extra prefetch depth applied on top of the usual budget when grouping. Chunks
# concentrate hard in the head of the ranking — one document can legitimately own
# a double-digit run of consecutive chunks — so filling N *distinct* documents
# needs materially more candidates than filling N chunks. Measured on a
# 550k-chunk corpus: a 400-chunk prefetch yielded ~300 distinct documents.
DOCUMENT_PREFETCH_FACTOR = 4

# Ceiling on the per-branch prefetch when grouping. The multipliers compound —
# the tool layer over-fetches 2x for verification headroom, _run_qdrant_query
# doubles again for dedup, and DOCUMENT_PREFETCH_FACTOR stacks on top — so an
# uncapped limit=100 asks Qdrant for 1600 candidates per branch, per doc_type.
# Measured on a 550k-chunk collection, same query and result count:
#
#   chunk    limit=100 (prefetch  400)   412 ms
#   document limit=100 (prefetch 1600)  4275 ms
#   document limit=100 (prefetch  800)   302 ms   <- identical 200 groups
#
# The extra depth past ~800 buys no additional documents and costs ~10x
# latency, so cap it. Only bites above limit=50; below that the computed
# budget is already under the ceiling.
MAX_DOCUMENT_PREFETCH = 800


def search_method_label(fusion: str) -> str:
    """The bounded ``search_method`` / metric label for a hybrid search.

    Clamps an unrecognised ``fusion`` to the default rather than interpolating
    it, so a caller-controlled string can never become a metric label. Callers
    that also CONSTRUCT the algorithm still pass the raw value, which continues
    to raise on an invalid mode — this only bounds what gets reported, it does
    not make a bad request succeed.
    """
    safe = fusion if fusion in VALID_FUSIONS else VALID_FUSIONS[0]
    return f"bm25_hybrid_{safe}"


class _FlattenedGroups:
    """Adapt a grouped Qdrant response to the ungrouped ``.points`` shape.

    Grouping is a retrieval concern, not a result-shape concern: every caller
    downstream (dedup, SearchResult construction, verification, context
    expansion) already works on a flat list of scored points. Flattening each
    group to its best hit here keeps that whole path granularity-agnostic
    instead of forking it.
    """

    __slots__ = ("points",)

    def __init__(self, groups):
        # ``hits`` is ordered best-first within a group, so hits[0] is the
        # document's strongest chunk. Empty groups cannot occur in practice
        # (a group exists because a point matched) but are skipped defensively.
        self.points = [g.hits[0] for g in groups if g.hits]


class BM25HybridSearchAlgorithm(SearchAlgorithm):
    """
    Hybrid search combining dense semantic vectors with BM25 sparse vectors.

    Uses Qdrant's native Reciprocal Rank Fusion (RRF) to automatically merge
    results from both dense (semantic) and sparse (BM25 keyword) searches.
    This provides the best of both worlds: semantic understanding for conceptual
    queries and precise keyword matching for specific terms, acronyms, and codes.

    The fusion happens efficiently in the database using the prefetch mechanism,
    eliminating the need for application-layer result merging.

    The collection may hold a mix of hybrid documents (dense + sparse) and
    keyword-only documents (sparse only, ``keyword-index`` tag). The dense
    prefetch simply never returns keyword-only points (they carry no dense
    vector); they surface via the sparse prefetch and are merged by fusion — so a
    single unified query covers both without any mode branch.
    """

    def __init__(self, score_threshold: float = 0.0, fusion: str = "rrf"):
        """
        Initialize BM25 hybrid search algorithm.

        Args:
            score_threshold: Minimum fusion score (default: 0.0 = no cut).
                           NOT a 0-1 relevance scale for either algorithm: RRF
                           scores are a rank artifact peaking around
                           2/VECTOR_SEARCH_RRF_K (~0.033 at the default k=60),
                           and DBSF sums normalized per-retriever scores so it
                           is unbounded above 1.0. Leave at 0.0 and cut by rank
                           via ``limit``.
            fusion: Fusion algorithm to use: "rrf" (Reciprocal Rank Fusion, default)
                   or "dbsf" (Distribution-Based Score Fusion).

        Raises:
            ValueError: If fusion is not "rrf" or "dbsf"
        """
        if fusion not in VALID_FUSIONS:
            raise ValueError(
                f"Invalid fusion algorithm '{fusion}'. Must be 'rrf' or 'dbsf'"
            )

        # super() sets the per-instance query_embedding / query_token_count
        # side-channel; this adds the cache key for it.
        super().__init__()
        self.score_threshold = score_threshold
        self.fusion = models.Fusion.RRF if fusion == "rrf" else models.Fusion.DBSF
        self.fusion_name = fusion
        # ``_embedded_query`` is the query string whose dense embedding is held
        # in ``query_embedding`` — repeated search() calls on this per-request
        # instance (the doc_types loop) reuse it instead of re-embedding.
        self._embedded_query: str | None = None

    @property
    def name(self) -> str:
        return "bm25_hybrid"

    @property
    def requires_vector_db(self) -> bool:
        return True

    async def _embed_query_dense(self, query: str, settings: Any) -> list | None:
        """Embed the query for the dense prefetch.

        Cached per query on this (per-request) instance: nc_semantic_search calls
        search() once per doc_type with the same query, so re-embedding each time
        would make N redundant API calls and bill the query's tokens N times
        (Deck #67). Reuse the first call's embedding + token count so the query is
        embedded — and metered — exactly once.
        """
        with trace_operation("search.get_provider"):
            provider = get_provider()
        with trace_operation("search.dense_embedding"):
            if self.query_embedding is not None and self._embedded_query == query:
                return self.query_embedding
            dense_embedding, query_tokens = await provider.embed_with_usage(query)
            # Store for reuse by callers (e.g. the PCA projection in
            # vector/visualization.py) and for the usage-metering hook in
            # server/semantic.py (token count).
            self.query_embedding = dense_embedding
            self.query_token_count = query_tokens
            self._embedded_query = query
            # Export query-embedding token cost to Prometheus (operation=query),
            # mirroring the per-search billing record in server/semantic.py.
            record_embedding_tokens(
                settings.get_embedding_provider_family(), "query", query_tokens
            )
        logger.debug("Generated dense embedding (dimension=%s)", len(dense_embedding))
        return dense_embedding

    def _build_fusion_query(self, settings: Any) -> Any:
        """Build the fusion stage that merges the dense and sparse prefetches.

        For RRF this is ``RrfQuery``, which carries an explicit ranking constant
        ``k``. The plain ``FusionQuery(fusion=RRF)`` Qdrant defaults to hardcodes
        k=2 (see ``qdrant_client/hybrid/fusion.py``:
        ``DEFAULT_RANKING_CONSTANT_K = 2``), giving ``score = 1/(rank + 2)``:
        adjacent ranks differ by 33%, so a point ranked 0 by ONE retriever
        (0.5) beats a point ranked 3 by BOTH (0.2 + 0.2 = 0.4). That inverts
        the purpose of fusion. ``VECTOR_SEARCH_RRF_K`` (default 60) restores
        the standard behaviour where cross-retriever agreement dominates.

        DBSF normalises score distributions rather than ranks and has no such
        constant, so it keeps the plain ``FusionQuery``.

        Note the resulting scores are much smaller (1/60 ≈ 0.0167 at rank 0
        instead of 0.5) and span a narrow band. That is inherent to RRF at any
        sane k: the fused score is a rank artifact, not a calibrated relevance
        measure, so it must not be used as an absolute relevance threshold.
        """
        if self.fusion is not models.Fusion.RRF:
            return models.FusionQuery(fusion=self.fusion)
        return models.RrfQuery(rrf=models.Rrf(k=settings.vector_search_rrf_k))

    async def _run_qdrant_query(
        self,
        qdrant_client: Any,
        settings: Any,
        *,
        sparse_query: models.SparseVector,
        dense_embedding: list | None,
        query_filter: Filter,
        limit: int,
        score_threshold: float,
        granularity: str = GRANULARITY_CHUNK,
    ) -> Any:
        """Execute the Qdrant query: dense + sparse prefetches merged by native
        fusion (RRF or DBSF).

        Keyword-only documents (``keyword-index`` tag) carry no dense vector, so
        the dense prefetch never returns them; they surface via the sparse
        prefetch and are merged in by fusion. No mode branch is needed for that.

        ``granularity="document"`` runs the same prefetch + fusion through
        Qdrant's native grouping so each document contributes exactly one row
        (its best chunk) instead of competing for slots chunk-by-chunk. The
        prefetch is deepened by ``DOCUMENT_PREFETCH_FACTOR`` because filling N
        distinct documents needs more candidates than filling N chunks.
        """
        collection_name = settings.get_collection_name()
        grouped = granularity == GRANULARITY_DOCUMENT
        # Both paths deepen the PREFETCH 2x (grouping deepens it further, bounded
        # by MAX_DOCUMENT_PREFETCH so the compounding multipliers can't produce a
        # pathological query at a high ``limit``). Note this is the prefetch
        # depth only -- the grouped branch's own ``limit`` must stay exact; see
        # the comment on that call.
        prefetch_limit = limit * 2
        if grouped:
            prefetch_limit = min(
                prefetch_limit * DOCUMENT_PREFETCH_FACTOR, MAX_DOCUMENT_PREFETCH
            )
        prefetches = [
            # Dense semantic search
            models.Prefetch(
                query=dense_embedding,
                using="dense",
                limit=prefetch_limit,
                filter=query_filter,
            ),
            # Sparse BM25 search
            models.Prefetch(
                query=sparse_query,
                using="sparse",
                limit=prefetch_limit,
                filter=query_filter,
            ),
        ]
        with trace_operation(
            "search.qdrant_query",
            attributes={
                # Must track what is actually requested below: the grouped
                # branch asks for exactly ``limit`` groups while the chunk
                # branch over-fetches 2x. A single hardcoded value here would
                # misreport one of them to anyone debugging limit/prefetch
                # behaviour from traces — which is precisely the kind of
                # investigation this span exists for.
                "query.limit": limit if grouped else limit * 2,
                "query.fusion": self.fusion_name,
                "query.granularity": granularity,
            },
        ):
            if grouped:
                response = await qdrant_client.query_points_groups(
                    collection_name=collection_name,
                    prefetch=prefetches,
                    query=self._build_fusion_query(settings),
                    # doc_id is the per-document key. Caveat: it is NOT unique
                    # across doc_types (a note 42 and a file 42 are distinct
                    # documents sharing an id), so a cross-app grouped search
                    # can collapse two such documents into one group. Qdrant
                    # groups on a single payload field, so avoiding this needs a
                    # composite key written at index time. The tool layer's
                    # per-doc_type loop is unaffected; only doc_types=None is.
                    group_by="doc_id",
                    # One row per document: the caller asked for documents, and
                    # the extra hits would just re-introduce the chunk-level
                    # concentration this mode exists to remove.
                    group_size=1,
                    # Groups, not chunks -- and deliberately NOT ``limit * 2``.
                    # The chunk path over-fetches 2x because chunks from one
                    # document collapse during dedup, so it needs spares;
                    # ``group_size=1`` already yields one row per document, so
                    # there is nothing here for spares to replace. Worse, asking
                    # for more groups than the prefetch can fill makes Qdrant
                    # widen its grouping search and REORDER the head rather than
                    # simply appending weaker tail results, so an over-request
                    # measurably degrades the top of the result set. The damage
                    # grows as the requested group count approaches the prefetch
                    # depth, which is why this must track ``limit`` exactly.
                    limit=limit,
                    score_threshold=score_threshold,
                    with_payload=True,
                    with_vectors=False,
                )
                return _FlattenedGroups(response.groups)
            return await qdrant_client.query_points(
                collection_name=collection_name,
                prefetch=prefetches,
                # Fusion query (RRF or DBSF based on initialization)
                query=self._build_fusion_query(settings),
                limit=limit * 2,  # Get extra for deduplication
                score_threshold=score_threshold,
                with_payload=True,
                with_vectors=False,  # Don't return vectors to save bandwidth
            )

    async def search(
        self,
        query: str,
        user_id: str,
        limit: int = 10,
        doc_type: str | None = None,
        *,
        accessible_owners: list[str] | None = None,
        modified_after: int | None = None,
        modified_before: int | None = None,
        path_prefix: str | None = None,
        path_prefixes: Iterable[str] | None = None,
        path_prefix_folder_ids: list[str] | None = None,
        shared_root_ids: list[str] | None = None,
        granularity: str = GRANULARITY_CHUNK,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """
        Execute hybrid search using dense + sparse vectors with native RRF fusion.

        Returns unverified results from Qdrant. Access verification is
        performed separately at the server tool layer via
        ``nextcloud_mcp_server.search.verification.verify_search_results``
        (see ADR-019).

        Deduplicates by (doc_id, doc_type, chunk_start_offset, chunk_end_offset)
        to show multiple chunks from the same document while avoiding duplicate chunks.

        Args:
            query: Natural language or keyword search query
            user_id: User ID for filtering
            limit: Maximum results to return
            doc_type: Optional document type filter
            accessible_owners: Owner UIDs the user can read (self + share
                senders), pre-computed by the caller from the OCS Sharing API.
                Defaults to ``[user_id]`` (self-only) when ``None``.
            modified_after: Inclusive lower bound on ``modified_at`` (Unix
                seconds, UTC); ``None`` ⇒ open-ended (ADR-027).
            modified_before: Inclusive upper bound on ``modified_at`` (Unix
                seconds, UTC); ``None`` ⇒ open-ended (ADR-027).
            path_prefix: Deprecated single folder filter; folded into
                ``path_prefixes`` (ADR-027 Phase 2).
            path_prefixes: Folder/path filters on ``file_path`` (files only),
                OR-ed together; ``None``/empty ⇒ no path filter (ADR-027
                Phase 2).
            granularity: ``"chunk"`` (default) returns the best-matching
                passages, so one document may occupy several rows. ``"document"``
                returns one row per document — its best chunk — which is the
                right shape for "which files mention X". Note this changes what
                ``limit`` counts (documents, not chunks); it does NOT deepen
                recall, since a document whose best chunk misses the prefetch is
                absent either way.
            **kwargs: Additional parameters (score_threshold override)

        Returns:
            List of unverified SearchResult objects ranked by RRF fusion score

        Raises:
            MCPError: If vector sync is not enabled or search fails
        """
        if granularity not in VALID_GRANULARITIES:
            # Validated here rather than only at the tool boundary so every
            # surface (MCP tool, /api/v1) gets the same contract, and an
            # unrecognised value fails loudly instead of silently falling back
            # to chunk granularity.
            raise ValueError(
                f"Invalid granularity {granularity!r}. "
                f"Must be one of {VALID_GRANULARITIES}"
            )

        settings = get_settings()
        score_threshold = kwargs.get("score_threshold", self.score_threshold)

        # Self-describing label reused across every log line below and the result
        # metadata: always "bm25_hybrid_<fusion>" — the query fuses dense + sparse
        # prefetches; keyword-only documents simply contribute via the sparse side.
        method_label = f"bm25_hybrid_{self.fusion_name}"

        logger.info(
            "%s: query='%s', user=%s, limit=%s, score_threshold=%s, doc_type=%s",
            method_label,
            query,
            user_id,
            limit,
            score_threshold,
            doc_type,
        )

        # Dense query embedding (fused with the sparse prefetch below).
        dense_embedding = await self._embed_query_dense(query, settings)

        # Generate sparse embedding for BM25 keyword search
        with trace_operation("search.get_bm25_service"):
            bm25_service = await get_bm25_service()
        with trace_operation("search.sparse_embedding_bm25"):
            sparse_embedding = await bm25_service.encode_async(query)
        logger.debug(
            "Generated sparse embedding (%s non-zero terms)",
            len(sparse_embedding["indices"]),
        )

        # Build Qdrant filter (placeholder + ACL + doc_type + modified_at range).
        # Shared with the dense-only SemanticSearchAlgorithm via the common
        # ADR-027 helper so every search surface applies one filter contract.
        filter_conditions = build_base_filter_conditions(
            user_id=user_id,
            accessible_owners=accessible_owners,
            doc_type=doc_type,
            modified_after=modified_after,
            modified_before=modified_before,
            path_prefix=path_prefix,
            path_prefixes=path_prefixes,
            path_prefix_folder_ids=path_prefix_folder_ids,
            shared_root_ids=shared_root_ids,
        )

        query_filter = Filter(must=filter_conditions)

        # Execute hybrid search with Qdrant native RRF fusion
        with trace_operation("search.get_qdrant_client"):
            qdrant_client = await get_qdrant_client()

        sparse_query = models.SparseVector(
            indices=sparse_embedding["indices"],
            values=sparse_embedding["values"],
        )
        try:
            search_response = await self._run_qdrant_query(
                qdrant_client,
                settings,
                sparse_query=sparse_query,
                dense_embedding=dense_embedding,
                query_filter=query_filter,
                limit=limit,
                score_threshold=score_threshold,
                granularity=granularity,
            )
            record_qdrant_operation("search", "success")
        except Exception:
            record_qdrant_operation("search", "error")
            raise

        logger.info(
            "Qdrant %s returned %s results (before deduplication)",
            method_label,
            len(search_response.points),
        )

        if search_response.points:
            # Log top 3 scores to help with threshold tuning. Neither algorithm
            # is on a 0-1 relevance scale: RRF peaks near 2/VECTOR_SEARCH_RRF_K
            # (~0.033 at k=60) and DBSF is unbounded above 1.0.
            top_scores = [p.score for p in search_response.points[:3]]
            logger.debug("Top 3 %s scores: %s", method_label, top_scores)

        # Deduplicate by (doc_id, doc_type, chunk_start, chunk_end)
        # This allows multiple chunks from same doc, but removes duplicate chunks
        with trace_operation(
            "search.deduplicate",
            attributes={"dedupe.num_points": len(search_response.points)},
        ):
            seen_chunks: set[tuple[str, str, Any, Any]] = set()
            results: list[SearchResult] = []
            # Reuse the label already computed for the logs above so the two
            # never drift (and to avoid the duplicate expression).
            metadata_extras = {"search_method": method_label}

            for point in search_response.points:
                sr = build_search_result_from_point(
                    point, metadata_extras=metadata_extras
                )
                if sr is None:
                    continue

                chunk_key = (
                    sr.id,
                    sr.doc_type,
                    sr.chunk_start_offset,
                    sr.chunk_end_offset,
                )
                if chunk_key in seen_chunks:
                    continue
                seen_chunks.add(chunk_key)

                results.append(sr)
                if len(results) >= limit:
                    break

        # Log the count only — NOT titles. These results are unverified: with
        # owner-level share expansion the candidate set can include other users'
        # documents that verify-on-read will drop, so titles must not be logged
        # until after verification (the verifying callers log verified titles).
        logger.info("Returning %s unverified results after deduplication", len(results))

        return results
