"""Semantic search algorithm using vector similarity (Qdrant)."""

import logging
from typing import Any

from httpx import HTTPStatusError
from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service
from nextcloud_mcp_server.observability.metrics import record_qdrant_operation
from nextcloud_mcp_server.search.algorithms import (
    NextcloudClientProtocol,
    SearchAlgorithm,
    SearchResult,
)
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
        nextcloud_client: NextcloudClientProtocol | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """Execute semantic search using vector similarity.

        Args:
            query: Natural language search query
            user_id: User ID for filtering
            limit: Maximum results to return
            doc_type: Optional document type filter (currently only "note" supported)
            nextcloud_client: NextcloudClient for access verification
            **kwargs: Additional parameters (score_threshold override)

        Returns:
            List of SearchResult objects ranked by similarity score

        Raises:
            McpError: If vector sync is not enabled or search fails
        """
        settings = get_settings()
        score_threshold = kwargs.get("score_threshold", self.score_threshold)

        logger.info(
            f"Semantic search: query='{query}', user={user_id}, "
            f"limit={limit}, score_threshold={score_threshold}, doc_type={doc_type}"
        )

        # Generate embedding for query
        embedding_service = get_embedding_service()
        query_embedding = await embedding_service.embed(query)
        logger.debug(
            f"Generated embedding for query (dimension={len(query_embedding)})"
        )

        # Build Qdrant filter
        filter_conditions = [
            FieldCondition(
                key="user_id",
                match=MatchValue(value=user_id),
            )
        ]

        # Add doc_type filter if specified
        if doc_type:
            filter_conditions.append(
                FieldCondition(
                    key="doc_type",
                    match=MatchValue(value=doc_type),
                )
            )

        # Search Qdrant
        qdrant_client = await get_qdrant_client()
        try:
            search_response = await qdrant_client.query_points(
                collection_name=settings.get_collection_name(),
                query=query_embedding,
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
            f"Qdrant returned {len(search_response.points)} results "
            f"(before deduplication and access verification)"
        )

        if search_response.points:
            # Log top 3 scores to help with threshold tuning
            top_scores = [p.score for p in search_response.points[:3]]
            logger.debug(f"Top 3 similarity scores: {top_scores}")

        # Deduplicate by document ID (multiple chunks per document)
        results = await self._deduplicate_and_verify(
            search_response.points, limit, nextcloud_client
        )

        logger.info(
            f"Returning {len(results)} results after deduplication and access verification"
        )
        if results:
            result_details = [
                f"{r.doc_type}_{r.id} (score={r.score:.3f}, title='{r.title}')"
                for r in results[:5]  # Show top 5
            ]
            logger.debug(f"Top results: {', '.join(result_details)}")

        return results

    async def _deduplicate_and_verify(
        self,
        points: list[Any],
        limit: int,
        nextcloud_client: NextcloudClientProtocol | None,
    ) -> list[SearchResult]:
        """Deduplicate results by (doc_id, doc_type) and verify access.

        Supports multiple document types with dispatch to appropriate client methods.
        Deduplication is now by (doc_id, doc_type) tuple to handle cases where
        the same ID might exist across different document types.

        Args:
            points: Qdrant search results
            limit: Maximum results to return
            nextcloud_client: NextcloudClient for access verification (optional)

        Returns:
            List of SearchResult objects
        """
        seen_docs = set()  # Track (doc_id, doc_type) tuples
        results = []

        for result in points:
            doc_id = int(result.payload["doc_id"])
            doc_type = result.payload.get("doc_type", "note")
            doc_key = (doc_id, doc_type)

            # Skip if we've already seen this document
            if doc_key in seen_docs:
                continue

            seen_docs.add(doc_key)

            # Verify access via Nextcloud API if client provided
            # Dispatch to appropriate client based on doc_type
            verified_result = None

            if nextcloud_client:
                verified_result = await self._verify_document_access(
                    nextcloud_client, doc_id, doc_type, result
                )

            if verified_result:
                results.append(verified_result)
            elif not nextcloud_client:
                # No access verification, return result directly
                results.append(
                    SearchResult(
                        id=doc_id,
                        doc_type=doc_type,
                        title=result.payload["title"],
                        excerpt=result.payload["excerpt"],
                        score=result.score,
                        metadata={
                            "chunk_index": result.payload.get("chunk_index"),
                            "total_chunks": result.payload.get("total_chunks"),
                        },
                    )
                )

            if len(results) >= limit:
                break

        return results

    async def _verify_document_access(
        self,
        nextcloud_client: NextcloudClientProtocol,
        doc_id: int,
        doc_type: str,
        qdrant_result: Any,
    ) -> SearchResult | None:
        """Verify user has access to a document via Nextcloud API.

        Dispatches to appropriate client method based on document type.

        Args:
            nextcloud_client: Client for API access
            doc_id: Document ID
            doc_type: Document type ("note", "file", "calendar", etc.)
            qdrant_result: Original Qdrant search result

        Returns:
            SearchResult if access verified, None if access denied or error
        """
        try:
            if doc_type == "note":
                note = await nextcloud_client.notes.get_note(doc_id)
                return SearchResult(
                    id=doc_id,
                    doc_type="note",
                    title=qdrant_result.payload["title"],
                    excerpt=qdrant_result.payload["excerpt"],
                    score=qdrant_result.score,
                    metadata={
                        "category": note.get("category", ""),
                        "chunk_index": qdrant_result.payload["chunk_index"],
                        "total_chunks": qdrant_result.payload["total_chunks"],
                    },
                )
            elif doc_type == "file":
                # Future: verify file access when files are indexed
                logger.info(
                    f"File {doc_id} found in search but file verification not yet implemented"
                )
                return None
            elif doc_type == "calendar":
                # Future: verify calendar access when calendar events are indexed
                logger.info(
                    f"Calendar event {doc_id} found in search but calendar verification not yet implemented"
                )
                return None
            else:
                logger.warning(
                    f"Unknown document type '{doc_type}' for doc_id {doc_id}"
                )
                return None

        except HTTPStatusError as e:
            if e.response.status_code in (403, 404):
                # User lost access or document deleted
                logger.debug(f"Skipping {doc_type} {doc_id}: {e.response.status_code}")
                return None
            else:
                # Log other errors but continue processing
                logger.warning(
                    f"Error verifying access to {doc_type} {doc_id}: {e.response.status_code}"
                )
                return None
