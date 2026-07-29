"""Pydantic models for semantic search responses."""

from pydantic import BaseModel, Field

from .base import BaseResponse


class SemanticSearchResult(BaseModel):
    """Model for semantic search results with additional metadata."""

    id: int = Field(
        description=(
            "Document ID. Numeric for all currently indexed types (notes, files, "
            "deck cards, news items). The internal SearchResult.id is stringified "
            "for Qdrant's keyword-indexed doc_id payload; the MCP response narrows "
            "back to int via int(r.id). A future doc_type with non-numeric ids "
            "would surface here as a TypeError at the narrowing boundary, "
            "forcing a deliberate widening of this field rather than a silent "
            "API change."
        )
    )
    doc_type: str = Field(
        description="Document type (note, calendar_event, deck_card, etc.)"
    )
    title: str = Field(description="Document title")
    category: str = Field(
        default="", description="Document category (notes) or location (calendar)"
    )
    excerpt: str = Field(description="Excerpt from matching chunk")
    score: float = Field(
        description=(
            "Relevance score (≥ 0.0, higher is better). A normalized fusion score "
            "— RRF in [0.0, 1.0], DBSF can exceed 1.0."
        )
    )
    chunk_index: int = Field(description="Index of matching chunk in document")
    total_chunks: int = Field(description="Total number of chunks in document")
    chunk_start_offset: int | None = Field(
        default=None, description="Character position where chunk starts in document"
    )
    chunk_end_offset: int | None = Field(
        default=None, description="Character position where chunk ends in document"
    )
    page_number: int | None = Field(
        default=None, description="First (or only) page for PDF documents"
    )
    page_end: int | None = Field(
        default=None,
        description="Last page for packed multi-page chunks; equals page_number "
        "for single-page chunks",
    )
    page_count: int | None = Field(
        default=None, description="Total number of pages in PDF document"
    )
    # Context expansion fields (optional, populated when include_context=True)
    has_context_expansion: bool = Field(
        default=False, description="Whether context expansion was performed"
    )
    marked_text: str | None = Field(
        default=None,
        description="Full text with position markers around matched chunk",
    )
    before_context: str | None = Field(
        default=None, description="Text before the matched chunk"
    )
    after_context: str | None = Field(
        default=None, description="Text after the matched chunk"
    )
    has_before_truncation: bool | None = Field(
        default=None, description="Whether before_context was truncated"
    )
    has_after_truncation: bool | None = Field(
        default=None, description="Whether after_context was truncated"
    )


class SemanticSearchResponse(BaseResponse):
    """Response model for semantic search across all indexed Nextcloud apps."""

    results: list[SemanticSearchResult] = Field(
        description="Semantic search results with similarity scores"
    )
    query: str = Field(description="The search query used")
    total_found: int = Field(description="Total number of documents found")
    search_method: str = Field(
        default="semantic",
        description=(
            "Search method used, e.g. 'bm25_hybrid_<fusion>' (dense + BM25 sparse "
            "fused). Keyword-only documents contribute via the sparse side of the "
            "same fused query."
        ),
    )
    verified_chunk_count: int = Field(
        default=0,
        description=(
            "Number of search result chunks that passed verify-on-read "
            "access checks (ADR-019). Equals len(verified_results) before "
            "trimming to limit. Sized in chunks (result rows), NOT in "
            "unique documents — see dropped_document_count for the "
            "per-document counterpart."
        ),
    )
    dropped_document_count: int = Field(
        default=0,
        description=(
            "Number of unique (doc_id, doc_type) pairs dropped as ghost "
            "records during verify-on-read (ADR-019). A short result page "
            "(len(results) < limit) combined with a non-zero "
            "dropped_document_count indicates ghost density rather than "
            "scarcity of relevant content. Note: this counter is sized in "
            "unique documents while verified_chunk_count is sized in "
            "chunks — a single document can contribute multiple chunks, "
            "so subtracting dropped_document_count from "
            "verified_chunk_count is NOT a meaningful operation."
        ),
    )


class VectorSyncStatusResponse(BaseResponse):
    """Response for vector sync status.

    Provides information about the current state of vector sync,
    including how many documents are indexed and how many are pending.

    Attributes:
        indexed_documents: Distinct documents indexed in the vector database
        indexed_chunks: Total indexed chunks (vector points); ~N per document
        indexed_count: DEPRECATED alias of indexed_chunks
        pending_count: Number of documents in processing queue
        status: Current sync status ("idle" or "syncing")
        enabled: Whether vector sync is enabled
    """

    indexed_documents: int = Field(
        default=0, description="Distinct documents indexed in the vector database"
    )
    indexed_chunks: int = Field(
        default=0, description="Total indexed chunks (vector points); ~N per document"
    )
    indexed_count: int = Field(
        default=0,
        description=(
            "DEPRECATED alias of indexed_chunks (the chunk/point count). Use "
            "indexed_documents for the distinct-document count."
        ),
    )
    pending_count: int = Field(
        default=0, description="Number of documents pending processing"
    )
    status: str = Field(
        default="disabled",
        description='Sync status: "idle", "syncing", or "disabled"',
    )
    enabled: bool = Field(default=False, description="Whether vector sync is enabled")
    ingest_queue: str | None = Field(
        default=None,
        description='Ingest queue backend: "memory" or "postgres" (Deck #183)',
    )
    job_counts: dict[str, int] | None = Field(
        default=None,
        description=(
            "Per-status ingest job counts (todo/doing/failed/…) on the postgres "
            "queue backend; None on the in-memory backend"
        ),
    )
    job_counts_by_queue: dict[str, dict[str, int]] | None = Field(
        default=None,
        description=(
            "Per-tier-queue ingest job counts {queue: {status: count}} on the "
            "postgres backend (Deck #323), so an operator can see whether work is "
            "backed up on ingest-fast vs waiting on ingest-structured/ingest-ocr; "
            "None on the in-memory backend"
        ),
    )
    hybrid_chunks: int = Field(
        default=0,
        description=(
            "Chunks indexed in hybrid mode (dense + sparse). These carry a dense "
            "vector and so drive the vector-RAM footprint; keyword-index chunks "
            "(indexed_chunks - hybrid_chunks) are sparse-only and cost no dense RAM."
        ),
    )
    estimated_vector_bytes: int = Field(
        default=0,
        description=(
            "Estimated dense-vector RAM footprint in bytes "
            "(hybrid_chunks * embedding_dim * 4 * hnsw_overhead) — the real "
            "hybrid-search cost driver, which source-byte billing does not capture."
        ),
    )


__all__ = [
    "SemanticSearchResult",
    "SemanticSearchResponse",
    "VectorSyncStatusResponse",
]
