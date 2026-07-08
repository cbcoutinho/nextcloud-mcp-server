"""BM25 hybrid search algorithm using Qdrant native RRF fusion."""

import logging
from collections.abc import Iterable
from typing import Any

from qdrant_client import models
from qdrant_client.models import Filter

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_bm25_service, get_embedding_service
from nextcloud_mcp_server.observability.metrics import (
    record_embedding_tokens,
    record_qdrant_operation,
)
from nextcloud_mcp_server.observability.tracing import trace_operation
from nextcloud_mcp_server.search.access_filter import build_base_filter_conditions
from nextcloud_mcp_server.search.algorithms import (
    SearchAlgorithm,
    SearchResult,
    build_search_result_from_point,
)
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


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
            score_threshold: Minimum fusion score (0-1, default: 0.0 to allow fusion scoring)
                           Note: Both RRF and DBSF produce normalized scores
            fusion: Fusion algorithm to use: "rrf" (Reciprocal Rank Fusion, default)
                   or "dbsf" (Distribution-Based Score Fusion).

        Raises:
            ValueError: If fusion is not "rrf" or "dbsf"
        """
        if fusion not in ("rrf", "dbsf"):
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
        with trace_operation("search.get_embedding_service"):
            embedding_service = get_embedding_service()
        with trace_operation("search.dense_embedding"):
            if self.query_embedding is not None and self._embedded_query == query:
                return self.query_embedding
            dense_embedding, query_tokens = await embedding_service.embed_with_usage(
                query
            )
            # Store for reuse by callers (e.g., viz_routes PCA visualization) and
            # for the usage-metering hook in server/semantic.py (token count).
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
    ) -> Any:
        """Execute the Qdrant query: dense + sparse prefetches merged by native
        fusion (RRF or DBSF).

        Keyword-only documents (``keyword-index`` tag) carry no dense vector, so
        the dense prefetch never returns them; they surface via the sparse
        prefetch and are merged in by fusion. No mode branch is needed.
        """
        collection_name = settings.get_collection_name()
        with trace_operation(
            "search.qdrant_query",
            attributes={"query.limit": limit * 2, "query.fusion": self.fusion_name},
        ):
            return await qdrant_client.query_points(
                collection_name=collection_name,
                prefetch=[
                    # Dense semantic search
                    models.Prefetch(
                        query=dense_embedding,
                        using="dense",
                        limit=limit * 2,  # Get extra for deduplication
                        filter=query_filter,
                    ),
                    # Sparse BM25 search
                    models.Prefetch(
                        query=sparse_query,
                        using="sparse",
                        limit=limit * 2,  # Get extra for deduplication
                        filter=query_filter,
                    ),
                ],
                # Fusion query (RRF or DBSF based on initialization)
                query=models.FusionQuery(fusion=self.fusion),
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
            **kwargs: Additional parameters (score_threshold override)

        Returns:
            List of unverified SearchResult objects ranked by RRF fusion score

        Raises:
            McpError: If vector sync is not enabled or search fails
        """
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
            # Log top 3 scores to help with threshold tuning — normalized fusion
            # scores (RRF in [0,1]; DBSF can exceed 1).
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
