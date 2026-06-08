"""Base interfaces and data structures for search algorithms."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from qdrant_client.models import Filter, ScoredPoint

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.search.access_filter import build_ownership_filter
from nextcloud_mcp_server.vector.placeholder import get_placeholder_filter
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


@runtime_checkable
class NextcloudClientProtocol(Protocol):
    """Protocol for Nextcloud client supporting multi-document search.

    This protocol defines the interface that search algorithms need from a
    Nextcloud client to access documents across different apps (Notes, Files,
    Calendar, etc.). The client provides access to app-specific sub-clients
    that handle the actual API calls.

    Document types (e.g., "note", "file", "calendar") are NOT 1:1 with apps.
    For example, the Notes app specializes in markdown files, while Files/WebDAV
    handles multiple file types. The abstraction is at the document type level.

    Search algorithms query Qdrant to determine which document types are actually
    indexed before attempting to access them, enabling graceful cross-app search.
    """

    username: str

    # App-specific clients that search algorithms dispatch to
    @property
    def notes(self) -> Any:
        """Notes client for accessing note documents."""
        ...

    @property
    def webdav(self) -> Any:
        """WebDAV client for accessing file documents."""
        ...

    @property
    def calendar(self) -> Any:
        """Calendar client for accessing event/task documents."""
        ...

    @property
    def contacts(self) -> Any:
        """Contacts client for accessing contact card documents."""
        ...

    @property
    def deck(self) -> Any:
        """Deck client for accessing deck card documents."""
        ...

    @property
    def cookbook(self) -> Any:
        """Cookbook client for accessing recipe documents."""
        ...

    @property
    def tables(self) -> Any:
        """Tables client for accessing table row documents."""
        ...

    @property
    def news(self) -> Any:
        """News client for accessing news item documents."""
        ...

    # Top-level client helper (not a sub-client) used by verify-on-read to
    # gate file results on current vector-index tag membership.
    async def find_files_by_tag(
        self, tag_name: str, mime_type_filter: str | None = None
    ) -> list[dict]:
        """Return files carrying ``tag_name`` (folders expanded by MIME)."""
        ...


async def get_indexed_doc_types(
    user_id: str, accessible_owners: list[str] | None = None
) -> set[str]:
    """Query Qdrant to get actually-indexed document types for a user.

    This enables search algorithms to check which document types are available
    before attempting to search/verify them, allowing graceful cross-app search.

    Args:
        user_id: User ID to filter by.
        accessible_owners: Owner UIDs the user may read (self + share senders),
            as computed by ``access_filter.list_accessible_owners``. When
            provided, doc-type discovery is ACL-aware and matches the same
            ownership scope as the actual search (so a share recipient discovers
            cross-user doc_types). When ``None`` (the default), discovery is
            **self-only** — a recipient won't see doc_types that exist only in
            another owner's shared content. Pass the expanded set for cross-user
            discovery.

    Returns:
        Set of document type strings (e.g., {"note", "file", "calendar"})

    Example:
        >>> types = await get_indexed_doc_types("alice")
        >>> if "note" in types:
        ...     # Search notes
    """

    settings = get_settings()

    qdrant_client = await get_qdrant_client()
    collection = settings.get_collection_name()

    # Use scroll to sample documents and extract doc_types
    # Note: This could be optimized with a facet/aggregation query if Qdrant adds support
    try:
        scroll_results, _next_offset = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=Filter(
                must=[
                    get_placeholder_filter(),  # Exclude placeholders from doc_type discovery
                    # ACL-aware ownership scope (owner_id IN owners OR legacy
                    # user_id == user_id), matching the real search filter.
                    build_ownership_filter(user_id, accessible_owners),
                ]
            ),
            limit=1000,  # Sample size to discover types
            with_payload=["doc_type"],
            with_vectors=False,  # Don't need vectors for type discovery
        )

        doc_types: set[str] = {
            str(point.payload.get("doc_type"))
            for point in scroll_results
            if point.payload and point.payload.get("doc_type")
        }

        logger.debug("Found indexed document types for user %s: %s", user_id, doc_types)
        return doc_types

    except Exception as e:
        logger.warning("Failed to query Qdrant for doc_types: %s", e)
        return set()


@dataclass
class SearchResult:
    """A single search result with metadata and score.

    Attributes:
        id: Document ID — always a string. Producers stringify their native
            ID before writing to Qdrant so the keyword payload index on
            ``doc_id`` matches every point regardless of source doc_type.
            Public response models (e.g. ``SemanticSearchResult``) re-narrow
            this back to ``int`` at the MCP boundary via ``int(r.id)`` —
            see ``server/semantic.py`` for the narrowing site.
        doc_type: Document type (note, file, calendar, contact, etc.)
        title: Document title
        excerpt: Content excerpt showing match context
        score: Relevance score (≥ 0.0, higher is better)
            - RRF fusion: scores in [0.0, 1.0]
            - DBSF fusion: scores can exceed 1.0 (sum of normalized scores)
        metadata: Additional algorithm-specific metadata
        chunk_start_offset: Character position where chunk starts (None if not available)
        chunk_end_offset: Character position where chunk ends (None if not available)
        page_number: Page number for PDF documents (None for other doc types)
        page_count: Total number of pages in PDF document (None for other doc types)
        chunk_index: Zero-based index of this chunk in the document
        total_chunks: Total number of chunks in the document
        point_id: Qdrant point ID for batch vector retrieval (None if not from Qdrant)
    """

    id: str
    doc_type: str
    title: str
    excerpt: str
    score: float
    metadata: dict[str, Any] | None = None
    chunk_start_offset: int | None = None
    chunk_end_offset: int | None = None
    page_number: int | None = None
    page_count: int | None = None
    chunk_index: int = 0
    total_chunks: int = 1
    point_id: str | None = None
    # Pre-normalization score, set by the visualization route before it rescales
    # ``score`` to [0, 1] for visual encoding (see auth/viz_routes.py).
    original_score: float | None = None

    def __post_init__(self):
        """Validate score is non-negative.

        Note: Different fusion methods produce different score ranges:
        - RRF (Reciprocal Rank Fusion): Bounded to [0.0, 1.0]
        - DBSF (Distribution-Based Score Fusion): Unbounded (can exceed 1.0)
          DBSF sums normalized scores from multiple systems, so scores can be
          1.5, 2.0, etc. when multiple systems agree a document is highly relevant.
        """
        if self.score < 0.0:
            raise ValueError(f"Score must be non-negative, got {self.score}")


def build_search_result_from_point(
    point: ScoredPoint,
    *,
    metadata_extras: dict[str, Any] | None = None,
) -> SearchResult | None:
    """Construct a SearchResult from a Qdrant ScoredPoint payload.

    Returns ``None`` when the payload is missing — callers should skip the
    point. The defensive ``str()`` coercion on ``doc_id`` covers legacy int
    payloads until the startup backfill has run everywhere (see
    ``vector/qdrant_client.py:_backfill_doc_id_to_string``).

    Args:
        point: A Qdrant ``ScoredPoint`` from a search response.
        metadata_extras: Algorithm-specific metadata merged into the result's
            ``metadata`` dict (e.g., ``{"search_method": "bm25_hybrid_rrf"}``).

    Returns:
        A populated ``SearchResult``, or ``None`` if ``point.payload`` is
        missing.
    """
    if point.payload is None:
        return None

    raw_doc_id = point.payload.get("doc_id")
    if raw_doc_id is None:
        logger.warning("Skipping point %s: missing doc_id in payload", point.id)
        return None
    doc_id = str(raw_doc_id)
    doc_type = point.payload.get("doc_type", "note")

    # Caller-supplied metadata is merged first; payload-derived common fields
    # (chunk_index, total_chunks) win in case of key collisions so they always
    # reflect the actual point.
    metadata: dict[str, Any] = dict(metadata_extras) if metadata_extras else {}
    metadata["chunk_index"] = point.payload.get("chunk_index")
    metadata["total_chunks"] = point.payload.get("total_chunks")

    # File-specific metadata for PDF viewer
    if doc_type == "file" and (path := point.payload.get("file_path")):
        metadata["path"] = path

    # Deck-card metadata for frontend URL construction and verify-on-read
    # (ADR-019) — both board_id and stack_id are required to call
    # deck.get_card without an O(boards × stacks) iteration fallback.
    if doc_type == "deck_card":
        if board_id := point.payload.get("board_id"):
            metadata["board_id"] = board_id
        if stack_id := point.payload.get("stack_id"):
            metadata["stack_id"] = stack_id

    return SearchResult(
        id=doc_id,
        doc_type=doc_type,
        title=point.payload.get("title", "Untitled"),
        excerpt=point.payload.get("excerpt", ""),
        score=point.score,
        metadata=metadata,
        chunk_start_offset=point.payload.get("chunk_start_offset"),
        chunk_end_offset=point.payload.get("chunk_end_offset"),
        page_number=point.payload.get("page_number"),
        page_count=point.payload.get("page_count"),
        chunk_index=point.payload.get("chunk_index", 0),
        total_chunks=point.payload.get("total_chunks", 1),
        point_id=str(point.id),
    )


class SearchAlgorithm(ABC):
    """Abstract base class for search algorithms.

    All search algorithms must implement the search() method with consistent
    interface, allowing them to be used interchangeably.

    Attributes:
        query_embedding: The query embedding generated during the last search.
            Available after search() completes for algorithms that use embeddings.
            Can be reused by callers to avoid redundant embedding generation.
        query_token_count: Token count of the query embedding request from the
            last search (provider-reported, or estimated). Set by algorithms
            that embed the query so the usage-metering hook can bill
            ``tokens_embedded`` by tokens (Deck #67). The instance is
            per-request, so this side-channel is concurrency-safe.
    """

    # Class-level defaults are a safety net; __init__ shadows them per instance.
    query_embedding: list[float] | None = None
    query_token_count: int | None = None

    def __init__(self) -> None:
        # Set the query-embedding side-channel as instance attributes so
        # concurrent SearchAlgorithm instances never share it through the
        # class-level defaults above — per-request isolation by construction,
        # not just by the convention that each subclass redeclares them.
        # Subclasses with their own __init__ should call super().__init__().
        self.query_embedding: list[float] | None = None
        self.query_token_count: int | None = None

    @abstractmethod
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
        """Execute search with the given parameters.

        Args:
            query: Search query string
            user_id: User ID for multi-tenant filtering
            limit: Maximum number of results to return
            doc_type: Optional document type filter (note, file, calendar, etc.)
            accessible_owners: Owner UIDs the user is allowed to read (self plus
                the owners of content shared with them), pre-computed from the
                OCS Sharing API by the caller. Declared explicitly — rather than
                buried in ``**kwargs`` — so a misspelled keyword is a type error
                instead of a silent fall back to self-only scope. ``None`` means
                self-only (``[user_id]``).
            modified_after: Optional inclusive lower bound on the document's
                ``modified_at`` payload field (Unix seconds, UTC). Declared
                explicitly for the same discoverability/type-safety reason as
                ``accessible_owners`` (ADR-027). ``None`` ⇒ open-ended.
            modified_before: Optional inclusive upper bound on ``modified_at``
                (Unix seconds, UTC). ``None`` ⇒ open-ended.
            path_prefix: Deprecated single folder/path filter; folded into
                ``path_prefixes``. Kept for backward compatibility.
            path_prefixes: Optional folder/path filters on the ``file_path``
                payload field (ADR-027 Phase 2), OR-ed together. Only
                ``doc_type == "file"`` points carry ``file_path``, so any
                non-empty value implicitly restricts results to files. ``None``
                or empty ⇒ no path filter.
            **kwargs: Algorithm-specific parameters

        Returns:
            List of SearchResult objects ranked by relevance

        Raises:
            McpError: If search fails or configuration is invalid
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Return algorithm name for identification."""
        pass

    @property
    def supports_scoring(self) -> bool:
        """Whether this algorithm provides meaningful relevance scores.

        Default: True. Override if algorithm doesn't support scoring.
        """
        return True

    @property
    def requires_vector_db(self) -> bool:
        """Whether this algorithm requires vector database.

        Default: False. Override for semantic search.
        """
        return False
