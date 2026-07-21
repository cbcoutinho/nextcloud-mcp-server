"""Semantic search algorithm using vector similarity (Qdrant)."""

import logging
from collections.abc import Iterable
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchAny

from nextcloud_mcp_server.acl_hash import accessible_hash_set
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service
from nextcloud_mcp_server.observability.metrics import record_qdrant_operation
from nextcloud_mcp_server.search.access_filter import build_base_filter_conditions
from nextcloud_mcp_server.search.algorithms import (
    SearchAlgorithm,
    SearchResult,
    build_search_result_from_point,
)
from nextcloud_mcp_server.vector.payload_keys import ACL_HASH
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


class SemanticSearchAlgorithm(SearchAlgorithm):
    """Semantic search using vector similarity in Qdrant.

    Searches documents by meaning rather than exact keywords using
    768-dimensional embeddings and cosine distance.
    """

    def __init__(self, score_threshold: float = 0.7):
        """Initialize semantic search algorithm.

        Args:
            score_threshold: Minimum similarity score (0-1, default: 0.7)
        """
        super().__init__()
        self.score_threshold = score_threshold

    @property
    def name(self) -> str:
        return "semantic"

    @property
    def requires_vector_db(self) -> bool:
        return True

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
        **kwargs: Any,
    ) -> list[SearchResult]:
        """Execute semantic search using vector similarity.

        Returns unverified results from Qdrant. Access verification is
        performed separately at the server tool layer via
        ``nextcloud_mcp_server.search.verification.verify_search_results``
        (see ADR-019).

        Deduplicates by (doc_id, doc_type, chunk_start_offset, chunk_end_offset)
        to show multiple chunks from the same document while avoiding duplicate chunks.

        Args:
            query: Natural language search query
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
            **kwargs:
                - score_threshold (float): override the instance default

        Returns:
            List of unverified SearchResult objects ranked by similarity score

        Raises:
            McpError: If vector sync is not enabled or search fails
        """
        settings = get_settings()
        score_threshold = kwargs.get("score_threshold", self.score_threshold)

        logger.info(
            "Semantic search: query='%s', user=%s, limit=%s, score_threshold=%s, doc_type=%s",
            query,
            user_id,
            limit,
            score_threshold,
            doc_type,
        )

        # Generate embedding for query
        embedding_service = get_embedding_service()
        query_embedding = await embedding_service.embed(query)
        # Store for reuse by callers (e.g., viz_routes PCA visualization)
        self.query_embedding = query_embedding
        logger.debug(
            "Generated embedding for query (dimension=%s)", len(query_embedding)
        )

        # Build Qdrant filter (placeholder + ACL + doc_type + modified_at range).
        # Shared with BM25HybridSearchAlgorithm via the common ADR-027 helper so
        # the dense-only (API/visualization) and hybrid (MCP tool) paths apply
        # one filter contract.
        filter_conditions = build_base_filter_conditions(
            user_id=user_id,
            accessible_owners=accessible_owners,
            doc_type=doc_type,
            modified_after=modified_after,
            modified_before=modified_before,
            path_prefix=path_prefix,
            path_prefixes=path_prefixes,
            path_prefix_folder_ids=path_prefix_folder_ids,
        )

        # ACL pre-filter (design §11), opt-in via ACL_PREFILTER_ENABLED and OFF
        # by default. Additive `must` condition — it can only narrow results,
        # never broaden them, and verify-on-read remains the correctness
        # backstop. Only enable after a real acl_hash backfill: a MatchAny on
        # acl_hash excludes points missing the key (legacy docs), so enabling
        # it on an un-backfilled collection would silently drop results.
        if settings.acl_prefilter_enabled:
            # Groups are not yet threaded into the search signature; user +
            # public principals are covered. Group support is a follow-up.
            accessible = accessible_hash_set(user_id)
            filter_conditions.append(
                FieldCondition(key=ACL_HASH, match=MatchAny(any=sorted(accessible)))
            )

        # Search Qdrant
        qdrant_client = await get_qdrant_client()
        try:
            search_response = await qdrant_client.query_points(
                collection_name=settings.get_collection_name(),
                query=query_embedding,
                using="dense",  # Use named dense vector (BM25 hybrid collections)
                query_filter=Filter(must=filter_conditions),
                limit=limit * 2,  # Get extra for deduplication
                score_threshold=score_threshold,
                with_payload=True,
                with_vectors=False,  # Don't return vectors to save bandwidth
            )
            record_qdrant_operation("search", "success")
        except Exception:
            record_qdrant_operation("search", "error")
            raise

        logger.info(
            "Qdrant returned %s results (before deduplication)",
            len(search_response.points),
        )

        if search_response.points:
            # Log top 3 scores to help with threshold tuning
            top_scores = [p.score for p in search_response.points[:3]]
            logger.debug("Top 3 similarity scores: %s", top_scores)

        # Deduplicate by (doc_id, doc_type, chunk_start, chunk_end)
        # This allows multiple chunks from same doc, but removes duplicate chunks
        seen_chunks: set[tuple[str, str, Any, Any]] = set()
        results: list[SearchResult] = []

        for point in search_response.points:
            sr = build_search_result_from_point(point)
            if sr is None:
                continue

            chunk_key = (sr.id, sr.doc_type, sr.chunk_start_offset, sr.chunk_end_offset)
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
