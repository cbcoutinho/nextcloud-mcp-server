"""Document chunking for large texts using LangChain text splitters."""

import logging
from dataclasses import dataclass
from typing import Any

import anyio
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)


@dataclass
class ChunkWithPosition:
    """A text chunk with its character position in the original document."""

    text: str
    start_offset: int  # Character position where chunk starts
    end_offset: int  # Character position where chunk ends (exclusive)
    page_number: int | None = None  # First (or only) page for PDF chunks (optional)
    page_end: int | None = None  # Last page for packed multi-page chunks; equals
    # ``page_number`` for single-page chunks. None for non-paginated content.
    metadata: dict | None = None  # Additional processor-specific metadata (optional)


class DocumentChunker:
    """Chunk large documents for optimal embedding using LangChain text splitters.

    Uses RecursiveCharacterTextSplitter which preserves semantic boundaries
    by splitting on sentence and paragraph boundaries before resorting to
    character-level splitting.
    """

    def __init__(self, chunk_size: int = 2048, overlap: int = 200):
        """
        Initialize document chunker.

        Args:
            chunk_size: Number of characters per chunk (default: 2048)
            overlap: Number of overlapping characters between chunks (default: 200)
        """
        self.chunk_size = chunk_size
        self.overlap = overlap

        # Initialize LangChain RecursiveCharacterTextSplitter
        # Uses hierarchical splitting to preserve semantic boundaries:
        # - Paragraphs (\n\n)
        # - Sentences (. ! ?)
        # - Words (spaces)
        # - Characters (last resort)
        # This prevents mid-sentence splitting while maintaining semantic coherence
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            add_start_index=True,  # Enable position tracking
            strip_whitespace=True,
        )

    async def chunk_text(self, content: str) -> list[ChunkWithPosition]:
        """
        Split text into overlapping chunks with position tracking.

        Uses LangChain's RecursiveCharacterTextSplitter to create chunks that
        preserve semantic boundaries by splitting at paragraphs and sentences
        before resorting to word or character-level splitting. This ensures
        sentences are kept intact. Preserves character positions for each chunk
        to enable precise document retrieval.

        Args:
            content: Text content to chunk

        Returns:
            List of chunks with their character positions in the original content
        """

        # Handle empty content - return single empty chunk for backward compatibility
        if not content:
            return [ChunkWithPosition(text="", start_offset=0, end_offset=0)]

        # Run CPU-bound text splitting in thread pool to avoid blocking event loop
        docs = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
            self.splitter.create_documents,
            [content],
        )

        # Convert LangChain Documents to ChunkWithPosition objects
        chunks = [
            ChunkWithPosition(
                text=doc.page_content,
                start_offset=doc.metadata.get("start_index", 0),
                end_offset=doc.metadata.get("start_index", 0) + len(doc.page_content),
            )
            for doc in docs
        ]

        logger.debug(
            "Chunked document into %s chunks (chunk_size=%s, overlap=%s)",
            len(chunks),
            self.chunk_size,
            self.overlap,
        )
        return chunks


class PageAwareChunker:
    """Page-first chunker for paginated documents (PDFs).

    Unlike :class:`DocumentChunker`, which splits the concatenated document text
    on character boundaries and is therefore page-agnostic, this chunker splits
    on the page boundaries FIRST and only falls back to character splitting for
    pages larger than ``chunk_size``. As a result:

    * No chunk ever spans a page boundary, so ``page_number`` is always exact
      and the stored excerpt never leads with a neighbouring page's text (the
      char-based path can bury a short page's content in the tail of a chunk
      whose majority — and thus :func:`assign_page_numbers` label — is the
      previous page).
    * Chunks-per-page is ``ceil(page_chars / chunk_size)``. When ``chunk_size``
      is at least the largest page, that is exactly one chunk per page, giving a
      predictable vector count (== page count), a flat per-page embedding cost,
      and zero cross-page overlap duplication.

    Page numbers are assigned inline, so callers must NOT additionally run
    ``assign_page_numbers`` on the result.

    When ``pack_pages`` is enabled, consecutive sub-budget pages are greedily
    merged into a single chunk (up to ``chunk_size``) instead of one chunk per
    page — the density fix for lean-page PDFs (forms, slides, lean prose) where
    the per-page floor mints a full dense vector per near-empty page (Deck #636).
    Packed chunks span a page RANGE: ``page_number`` is the first page and
    ``page_end`` the last, so page-level citation survives packing. The
    single-page invariant above holds only when ``pack_pages`` is False.
    """

    def __init__(
        self, chunk_size: int = 2048, overlap: int = 200, pack_pages: bool = False
    ):
        """
        Initialize page-aware chunker.

        Args:
            chunk_size: Number of characters per chunk (default: 2048). Pages at
                or below this size become a single chunk (or, with
                ``pack_pages``, are merged with neighbours up to this budget);
                larger pages are character-split (with overlap) within the page
                only.
            overlap: Overlapping characters between sub-chunks of an oversized
                page (default: 200). Pages that fit in one chunk carry no
                overlap.
            pack_pages: When True, greedily merge consecutive sub-budget pages
                into one chunk (page-range citation preserved via
                ``page_number``/``page_end``). Default False keeps the legacy
                one-chunk-per-page behaviour.
        """
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.pack_pages = pack_pages

        # Only used for pages that exceed chunk_size. Same hierarchical splitter
        # as DocumentChunker so oversized pages keep semantic-boundary splitting.
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            add_start_index=True,
            strip_whitespace=True,
        )

    async def chunk_text(
        self, content: str, page_boundaries: list[dict[str, Any]]
    ) -> list[ChunkWithPosition]:
        """
        Split ``content`` into per-page chunks using ``page_boundaries``.

        Args:
            content: Full document text. Offsets in ``page_boundaries`` must
                index into this string (the extractor contract — see
                ``document_processors``).
            page_boundaries: Ordered list of ``{"page", "start_offset",
                "end_offset"}`` dicts. When empty, falls back to plain
                character chunking (no page numbers), matching
                :class:`DocumentChunker` for non-paginated input.

        Returns:
            List of chunks with character positions and ``page_number`` set.
        """
        if not content:
            return [ChunkWithPosition(text="", start_offset=0, end_offset=0)]

        # No page info — degrade to char-based behaviour so the class is safe to
        # call directly. The vector-sync processor pre-filters this case
        # (``should_use_page_aware`` requires a truthy boundary list), so in
        # production this branch is only reached by direct callers/tests, not
        # the indexing path.
        if not page_boundaries:
            docs = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                self.splitter.create_documents,
                [content],
            )
            return [
                ChunkWithPosition(
                    text=doc.page_content,
                    start_offset=doc.metadata.get("start_index", 0),
                    end_offset=doc.metadata.get("start_index", 0)
                    + len(doc.page_content),
                )
                for doc in docs
            ]

        splitter = (
            self._chunk_by_page_packed if self.pack_pages else self._chunk_by_page
        )
        chunks = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
            splitter,
            content,
            page_boundaries,
        )

        logger.debug(
            "Page-aware chunked document into %s chunks across %s pages "
            "(chunk_size=%s, overlap=%s, pack_pages=%s)",
            len(chunks),
            len(page_boundaries),
            self.chunk_size,
            self.overlap,
            self.pack_pages,
        )
        return chunks

    def _split_oversized_page(
        self, page_text: str, start: int, page: int
    ) -> list[ChunkWithPosition]:
        """Character-split a page that exceeds the budget within its own boundary.

        Offsets stay absolute (into ``content``) and the page number is fixed —
        an oversized page never spans a page range, so ``page_end == page``.
        """
        chunks: list[ChunkWithPosition] = []
        for doc in self.splitter.create_documents([page_text]):
            sub_start = start + doc.metadata.get("start_index", 0)
            chunks.append(
                ChunkWithPosition(
                    text=doc.page_content,
                    start_offset=sub_start,
                    end_offset=sub_start + len(doc.page_content),
                    page_number=page,
                    page_end=page,
                )
            )
        return chunks

    def _chunk_by_page(
        self, content: str, page_boundaries: list[dict[str, Any]]
    ) -> list[ChunkWithPosition]:
        """CPU-bound per-page splitting (runs in a worker thread)."""
        chunks: list[ChunkWithPosition] = []
        for boundary in page_boundaries:
            page = boundary["page"]
            start = boundary["start_offset"]
            end = boundary["end_offset"]
            page_text = content[start:end]

            # Skip blank pages: embedding an empty/whitespace-only string wastes
            # a provider call and a vector slot.
            if not page_text.strip():
                continue

            if len(page_text) <= self.chunk_size:
                stripped = page_text.strip()
                # Tighten offsets to the stripped text so they stay meaningful
                # even though the whole page is one chunk.
                lead = len(page_text) - len(page_text.lstrip())
                chunk_start = start + lead
                chunks.append(
                    ChunkWithPosition(
                        text=stripped,
                        start_offset=chunk_start,
                        end_offset=chunk_start + len(stripped),
                        page_number=page,
                        page_end=page,
                    )
                )
                continue

            # Oversized page: split within the page only.
            chunks.extend(self._split_oversized_page(page_text, start, page))
        return chunks

    def _chunk_by_page_packed(
        self, content: str, page_boundaries: list[dict[str, Any]]
    ) -> list[ChunkWithPosition]:
        """Greedy page-packing variant of :meth:`_chunk_by_page`.

        Merges consecutive sub-budget pages into a single chunk (spanning a page
        range) instead of emitting one chunk per page. Oversized pages still
        split within-page (they can't join a pack); blank pages are still
        skipped. Because the extractor joins pages with no separator, a pack is
        exactly the contiguous slice ``content[pack_start:pack_end]`` and the
        pre-strip span is bounded by ``chunk_size`` (no chunk exceeds budget).
        Runs in a worker thread (CPU-bound).
        """
        chunks: list[ChunkWithPosition] = []
        # Open pack: character span into ``content`` + covered page range.
        pack_start: int | None = None
        pack_end = 0
        first_page = 0
        last_page = 0

        def flush() -> None:
            nonlocal pack_start
            if pack_start is None:
                return
            raw = content[pack_start:pack_end]
            stripped = raw.strip()
            # A pack only opens on a non-blank page, so ``stripped`` is normally
            # non-empty; the guard keeps an all-whitespace pack from minting a
            # vector (parity with the blank-page skip).
            if stripped:
                lead = len(raw) - len(raw.lstrip())
                chunk_start = pack_start + lead
                chunks.append(
                    ChunkWithPosition(
                        text=stripped,
                        start_offset=chunk_start,
                        end_offset=chunk_start + len(stripped),
                        page_number=first_page,
                        page_end=last_page,
                    )
                )
            pack_start = None

        for boundary in page_boundaries:
            page = boundary["page"]
            start = boundary["start_offset"]
            end = boundary["end_offset"]
            page_text = content[start:end]

            # Skip blank pages (no wasted vector). A blank page interior to a
            # pack contributes only whitespace to the contiguous slice.
            if not page_text.strip():
                continue

            if len(page_text) > self.chunk_size:
                # An oversized page cannot join a pack: flush the pending pack,
                # then character-split the page within its own boundary.
                flush()
                chunks.extend(self._split_oversized_page(page_text, start, page))
                continue

            if pack_start is None:
                # Open a new pack on this page.
                pack_start, pack_end = start, end
                first_page = last_page = page
            elif end - pack_start <= self.chunk_size:
                # Extend the pack to include this contiguous page.
                pack_end = end
                last_page = page
            else:
                # Adding this page would overflow the budget: flush and reopen.
                flush()
                pack_start, pack_end = start, end
                first_page = last_page = page

        flush()
        return chunks
