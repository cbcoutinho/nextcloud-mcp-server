"""Exhaustive phrase match: ``algorithm="exact"`` on the search API.

Every document the user can read whose indexed text contains the query as a
phrase (case-insensitive), or whose file path contains its words. Not ranked:
it is a Qdrant ``scroll`` with a filter, not a vector query, so it has no top-k,
no prefetch ceiling, and ``total_found`` is the real number of documents.

This is what a subject access request needs: "every document that mentions
this person", which ranked retrieval cannot promise at any page size.

Results are one row per document (its first matching chunk), all with score
1.0. The phrase match needs the ``excerpt`` TEXT index with phrase matching
(``vector/qdrant_client.py``); Qdrant without it scans, which is slower but
still correct, except in strict mode, where the filter is refused.
"""

import logging
from collections.abc import Iterable
from typing import Any

from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchPhrase,
    MatchText,
    ScoredPoint,
)

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.search.access_filter import build_base_filter_conditions
from nextcloud_mcp_server.search.algorithms import (
    SearchAlgorithm,
    SearchResult,
    build_search_result_from_point,
)
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)

# ponytail: a hard ceiling so one query cannot hold an unbounded result set in
# memory; a case holds far fewer items (MAX_CASE_ITEMS) anyway. `truncated`
# tells the caller when it is hit. Stream pages if a real need exceeds it.
MAX_EXACT_DOCUMENTS = 20_000
# The same guard for chunks: a phrase repeated across thousands of chunks of a
# few documents would otherwise scroll every one of them (with its text) before
# reaching the document cap.
MAX_EXACT_CHUNKS = 10 * MAX_EXACT_DOCUMENTS
_SCROLL_PAGE = 1000
_PAYLOAD = [
    "doc_id",
    "doc_type",
    "title",
    "excerpt",
    "file_path",
    "board_id",
    "stack_id",
    "chunk_index",
    "total_chunks",
    "chunk_start_offset",
    "chunk_end_offset",
    "page_number",
    "page_end",
    "page_count",
]


def phrase_filter(query: str) -> Filter:
    """The query as a phrase in the chunk text, or its words in the file path."""
    return Filter(
        should=[
            FieldCondition(key="excerpt", match=MatchPhrase(phrase=query)),
            FieldCondition(key="file_path", match=MatchText(text=query)),
        ]
    )


class ExactMatchAlgorithm(SearchAlgorithm):
    """Every matching document, unranked. ``truncated`` is set after a search
    that stopped at ``max_documents`` or ``max_chunks``.

    The search API runs one search per doc type on the same instance, so there
    ``truncated`` means "some type was cut": each search only ever sets it.
    Reset it before reusing the instance for an unrelated query.
    """

    def __init__(
        self,
        max_documents: int = MAX_EXACT_DOCUMENTS,
        max_chunks: int = MAX_EXACT_CHUNKS,
    ) -> None:
        super().__init__()
        self.max_documents = max_documents
        self.max_chunks = max_chunks
        self.truncated = False

    @property
    def name(self) -> str:
        return "exact"

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
        """Every match; ``limit`` is ignored, since the point is not to cut."""
        query = query.strip()
        if not query:
            return []
        conditions = build_base_filter_conditions(
            user_id=user_id,
            accessible_owners=accessible_owners,
            doc_type=doc_type,
            modified_after=modified_after,
            modified_before=modified_before,
            path_prefix=path_prefix,
            path_prefixes=path_prefixes,
            path_prefix_folder_ids=kwargs.get("path_prefix_folder_ids"),
            shared_root_ids=kwargs.get("shared_root_ids"),
        )
        scroll_filter = Filter(must=[*conditions, phrase_filter(query)])
        qdrant = await get_qdrant_client()
        collection = get_settings().get_collection_name()

        found: dict[tuple[str, str], SearchResult] = {}
        scanned = 0
        offset = None
        while True:
            points, offset = await qdrant.scroll(
                collection_name=collection,
                scroll_filter=scroll_filter,
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=_PAYLOAD,
                with_vectors=False,
            )
            scanned += len(points)
            for point in points:
                # Truncated only once a document beyond the cap turns up, so a
                # match of exactly max_documents is complete. At a cap, the
                # documents kept are those scrolled first (point id order), not
                # the first by title: deterministic, so pages stay stable, but
                # an arbitrary subset, hence `truncated`.
                room = len(found) < self.max_documents
                if not _keep_first_chunk(found, point, room=room):
                    self.truncated = True
                    return list(found.values())
            if offset is None:
                return list(found.values())
            if scanned >= self.max_chunks:
                self.truncated = True
                return list(found.values())


def _keep_first_chunk(
    found: dict[tuple[str, str], SearchResult], point: Any, *, room: bool
) -> bool:
    """Record ``point`` under its document unless an earlier chunk is there.

    A shared file can be indexed under several owners, and a document matches
    in several chunks: one row per document, its first matching chunk.
    Returns False, recording nothing, for a new document when there is no
    ``room`` for it.
    """
    result = build_search_result_from_point(
        ScoredPoint(id=point.id, version=0, score=1.0, payload=point.payload),
        metadata_extras={"search_method": "exact"},
    )
    if result is None:
        logger.debug("Exact match skipped point %s: no usable payload", point.id)
        return True
    key = (result.doc_type, result.id)
    kept = found.get(key)
    if kept is None and not room:
        return False
    if kept is None or result.chunk_index < kept.chunk_index:
        found[key] = result
    return True
