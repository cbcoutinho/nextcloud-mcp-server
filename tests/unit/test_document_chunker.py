"""Unit tests for DocumentChunker with LangChain text splitters."""

import pytest

from nextcloud_mcp_server.vector.document_chunker import (
    ChunkWithPosition,
    DocumentChunker,
    PageAwareChunker,
)

pytestmark = pytest.mark.unit


def _make_doc(pages: list[str]) -> tuple[str, list[dict]]:
    """Build (full_text, page_boundaries) the way the PDF extractors do.

    Page texts are concatenated with no separator and boundaries index exactly
    into the result (the pypdfium2_fast contract).
    """
    content = ""
    boundaries: list[dict] = []
    offset = 0
    for i, text in enumerate(pages, start=1):
        boundaries.append(
            {"page": i, "start_offset": offset, "end_offset": offset + len(text)}
        )
        content += text
        offset += len(text)
    return content, boundaries


class TestDocumentChunkerPositions:
    """Test suite for DocumentChunker position tracking functionality."""

    async def test_single_chunk_simple_text(self):
        """Test that single-chunk documents return correct positions."""
        chunker = DocumentChunker(chunk_size=2048, overlap=200)
        content = "This is a short document."

        chunks = await chunker.chunk_text(content)

        assert len(chunks) == 1
        assert isinstance(chunks[0], ChunkWithPosition)
        assert chunks[0].text == content
        assert chunks[0].start_offset == 0
        assert chunks[0].end_offset == len(content)

    async def test_multiple_chunks_positions(self):
        """Test that multi-chunk documents have correct positions."""
        # Use small chunk size to force multiple chunks
        chunker = DocumentChunker(chunk_size=50, overlap=10)
        # Create content longer than chunk size
        content = (
            "This is the first sentence with some important content. "
            "This is the second sentence with more details. "
            "This is the third sentence continuing the discussion. "
            "This is the fourth sentence adding more context."
        )

        chunks = await chunker.chunk_text(content)

        # Verify we got multiple chunks
        assert len(chunks) > 1

        # Verify all chunks are ChunkWithPosition
        for chunk in chunks:
            assert isinstance(chunk, ChunkWithPosition)

        # Verify first chunk starts at 0
        assert chunks[0].start_offset == 0

        # Verify last chunk ends at content length
        assert chunks[-1].end_offset == len(content)

        # Verify chunks are contiguous or overlap (minimal gaps allowed)
        for i in range(len(chunks) - 1):
            # Next chunk should start at or near current chunk end
            # Allow small gaps (1-2 chars) for whitespace/punctuation at boundaries
            gap = chunks[i + 1].start_offset - chunks[i].end_offset
            assert gap <= 2, f"Gap too large between chunks: {gap} characters"

        # Verify we can reconstruct the content using positions
        for chunk in chunks:
            extracted = content[chunk.start_offset : chunk.end_offset]
            assert extracted == chunk.text

    async def test_chunk_positions_with_whitespace(self):
        """Test position tracking with various whitespace."""
        chunker = DocumentChunker(chunk_size=30, overlap=5)
        content = "First sentence here.  Second sentence.\n\nThird sentence.\tFourth sentence."

        chunks = await chunker.chunk_text(content)

        # Verify positions correctly handle whitespace
        for chunk in chunks:
            extracted = content[chunk.start_offset : chunk.end_offset]
            assert extracted == chunk.text
            # LangChain strips whitespace by default
            assert len(chunk.text.strip()) > 0

    async def test_empty_content(self):
        """Test that empty content returns empty chunk."""
        chunker = DocumentChunker(chunk_size=2048, overlap=200)
        content = ""

        chunks = await chunker.chunk_text(content)

        assert len(chunks) == 1
        assert chunks[0].text == ""
        assert chunks[0].start_offset == 0
        assert chunks[0].end_offset == 0

    async def test_chunk_overlap_positions(self):
        """Test that overlapping chunks have correct positions."""
        chunker = DocumentChunker(chunk_size=50, overlap=15)
        content = (
            "This is sentence one with content. "
            "This is sentence two with more. "
            "This is sentence three continuing. "
            "This is sentence four adding details."
        )

        chunks = await chunker.chunk_text(content)

        # Verify overlap exists if we have multiple chunks
        if len(chunks) > 1:
            for i in range(len(chunks) - 1):
                current_chunk = chunks[i]
                next_chunk = chunks[i + 1]

                # Verify positions are valid
                assert next_chunk.start_offset >= 0
                assert current_chunk.end_offset <= len(content)

                # With overlap, next chunk may start before current ends
                assert next_chunk.start_offset <= current_chunk.end_offset

    async def test_unicode_content_positions(self):
        """Test position tracking with Unicode characters."""
        chunker = DocumentChunker(chunk_size=50, overlap=10)
        content = (
            "Hello 世界. こんにちは there. мир Привет world. שלום مرحبا 你好 friend."
        )

        chunks = await chunker.chunk_text(content)

        # Verify all chunks extract correctly
        for chunk in chunks:
            extracted = content[chunk.start_offset : chunk.end_offset]
            assert extracted == chunk.text

        # Verify full coverage
        if len(chunks) == 1:
            assert chunks[0].start_offset == 0
            assert chunks[0].end_offset == len(content)

    async def test_realistic_note_content(self):
        """Test with realistic note content similar to Nextcloud Notes."""
        chunker = DocumentChunker(chunk_size=200, overlap=50)
        content = """My Project Notes

This is a note about my project. It contains several paragraphs of text
that should be chunked appropriately for embedding.

## Key Points

- First important point with some details
- Second point that needs to be remembered
- Third point for future reference

The document continues with more content here. We want to make sure that
the chunking preserves context across boundaries while maintaining proper
position tracking for each chunk.

This allows us to highlight the exact chunk that matched a search query,
which builds trust in the RAG system."""

        chunks = await chunker.chunk_text(content)

        # Should have multiple chunks
        assert len(chunks) > 1

        # Verify all chunks
        for chunk in chunks:
            assert isinstance(chunk, ChunkWithPosition)
            # Verify extraction
            extracted = content[chunk.start_offset : chunk.end_offset]
            assert extracted == chunk.text
            # Verify positions are valid
            assert chunk.start_offset >= 0
            assert chunk.end_offset <= len(content)
            assert chunk.start_offset < chunk.end_offset

    async def test_semantic_boundary_preservation(self):
        """Test that LangChain creates semantically coherent chunks."""
        chunker = DocumentChunker(chunk_size=100, overlap=20)
        content = (
            "First sentence is here. "
            "Second sentence follows. "
            "Third sentence continues. "
            "Fourth sentence ends."
        )

        chunks = await chunker.chunk_text(content)

        # Verify all chunks are extractable using their positions
        for chunk in chunks:
            extracted = content[chunk.start_offset : chunk.end_offset]
            assert extracted == chunk.text

            # Verify chunk text is meaningful (not empty or just whitespace)
            assert len(chunk.text.strip()) > 0

            # Verify positions are valid
            assert chunk.start_offset >= 0
            assert chunk.end_offset <= len(content)
            assert chunk.start_offset < chunk.end_offset

    async def test_paragraph_boundary_preservation(self):
        """Test that LangChain preserves paragraph boundaries."""
        chunker = DocumentChunker(chunk_size=80, overlap=15)
        content = """First paragraph here.

Second paragraph here.

Third paragraph here.

Fourth paragraph here."""

        chunks = await chunker.chunk_text(content)

        # LangChain should prefer splitting at paragraph boundaries (\n\n)
        # Verify we got multiple chunks
        assert len(chunks) >= 1

        # Verify all positions work correctly
        for chunk in chunks:
            extracted = content[chunk.start_offset : chunk.end_offset]
            assert extracted == chunk.text

    async def test_default_parameters(self):
        """Test that default parameters work correctly."""
        chunker = DocumentChunker()  # Use defaults: 2048 chars, 200 overlap

        # Create content that's smaller than default chunk size
        content = (
            "This is a short note with a few sentences. It should fit in one chunk."
        )

        chunks = await chunker.chunk_text(content)

        assert len(chunks) == 1
        assert chunks[0].text == content
        assert chunks[0].start_offset == 0
        assert chunks[0].end_offset == len(content)

    async def test_large_document_chunking(self):
        """Test chunking of a large document."""
        chunker = DocumentChunker(chunk_size=100, overlap=20)

        # Create a large document with multiple paragraphs
        paragraphs = [
            f"This is paragraph {i} with some meaningful content about topic {i}. "
            f"It contains multiple sentences to make it realistic. "
            f"The content should be properly chunked."
            for i in range(10)
        ]
        content = "\n\n".join(paragraphs)

        chunks = await chunker.chunk_text(content)

        # Should create multiple chunks
        assert len(chunks) > 1

        # Verify all chunks are valid
        for chunk in chunks:
            assert isinstance(chunk, ChunkWithPosition)
            assert len(chunk.text) > 0
            # Verify extraction
            extracted = content[chunk.start_offset : chunk.end_offset]
            assert extracted == chunk.text

        # Verify first and last positions
        assert chunks[0].start_offset == 0
        assert chunks[-1].end_offset == len(content)

    async def test_position_tracking_with_overlap(self):
        """Test that position tracking works correctly with overlap."""
        chunker = DocumentChunker(chunk_size=50, overlap=15)
        content = "A" * 25 + ". " + "B" * 25 + ". " + "C" * 25 + ". " + "D" * 25 + "."

        chunks = await chunker.chunk_text(content)

        if len(chunks) > 1:
            # Verify overlap creates correct positions
            for i in range(len(chunks) - 1):
                # Each chunk should be extractable
                assert (
                    content[chunks[i].start_offset : chunks[i].end_offset]
                    == chunks[i].text
                )

                # Next chunk should overlap with current
                # (start before current ends)
                if chunks[i + 1].start_offset < chunks[i].end_offset:
                    # There is overlap - verify content matches
                    overlap_start = chunks[i + 1].start_offset
                    overlap_end = chunks[i].end_offset
                    overlap_text = content[overlap_start:overlap_end]
                    assert overlap_text in chunks[i].text
                    assert overlap_text in chunks[i + 1].text


class TestPageAwareChunker:
    """Test suite for the page-aware chunker."""

    async def test_one_chunk_per_page_when_chunk_size_exceeds_pages(self):
        """chunk_size >= largest page => exactly one chunk per page."""
        pages = [
            "Page one content.",
            "Page two has rather more text than the first page does.",
            "Third.",
        ]
        content, boundaries = _make_doc(pages)

        chunks = await PageAwareChunker(chunk_size=2048, overlap=200).chunk_text(
            content, boundaries
        )

        # Predictable vector count == page count.
        assert len(chunks) == len(pages)
        for i, chunk in enumerate(chunks):
            assert chunk.page_number == i + 1
            # No leading/trailing whitespace in these pages -> text == page text.
            assert chunk.text == pages[i]
            # Offsets remain exact against the original document.
            assert content[chunk.start_offset : chunk.end_offset] == chunk.text

    async def test_no_chunk_spans_a_page_boundary(self):
        """Every chunk's character range stays within one page (the invariant)."""
        pages = [
            "Short.",
            "word " * 200,  # oversized page -> will be split within the page
            "Another short page of text.",
            "   ",  # blank page
            "Final page content here.",
        ]
        content, boundaries = _make_doc(pages)

        chunks = await PageAwareChunker(chunk_size=200, overlap=20).chunk_text(
            content, boundaries
        )

        for chunk in chunks:
            assert chunk.page_number is not None
            pb = boundaries[chunk.page_number - 1]
            assert pb["start_offset"] <= chunk.start_offset
            assert chunk.end_offset <= pb["end_offset"]
            assert content[chunk.start_offset : chunk.end_offset] == chunk.text

    async def test_oversized_page_splits_others_stay_single(self):
        """Only the oversized page yields multiple chunks; page numbers fixed."""
        pages = ["small page one", "word " * 200, "small page three"]
        content, boundaries = _make_doc(pages)

        chunks = await PageAwareChunker(chunk_size=200, overlap=20).chunk_text(
            content, boundaries
        )

        per_page = {1: 0, 2: 0, 3: 0}
        for chunk in chunks:
            per_page[chunk.page_number] += 1
        assert per_page[1] == 1
        assert per_page[2] > 1
        assert per_page[3] == 1

    async def test_oversized_page_with_leading_whitespace_offsets(self):
        """Offset invariant holds for oversized-page sub-chunks with leading ws.

        Guards the ``start + start_index`` path: LangChain's start_index points
        at the first non-whitespace char, so offsets must still extract exactly.
        """
        pages = ["  \n  " + "word " * 200, "Tail page."]
        content, boundaries = _make_doc(pages)

        chunks = await PageAwareChunker(chunk_size=200, overlap=20).chunk_text(
            content, boundaries
        )

        page_one = [c for c in chunks if c.page_number == 1]
        assert len(page_one) > 1  # oversized page really did split
        for chunk in chunks:
            assert chunk.page_number is not None
            assert content[chunk.start_offset : chunk.end_offset] == chunk.text
            pb = boundaries[chunk.page_number - 1]
            assert pb["start_offset"] <= chunk.start_offset
            assert chunk.end_offset <= pb["end_offset"]

    async def test_blank_pages_skipped(self):
        """Whitespace-only pages produce no chunks (no wasted embeddings)."""
        pages = ["Real content here.", "   \n  ", "More real content."]
        content, boundaries = _make_doc(pages)

        chunks = await PageAwareChunker(chunk_size=2048, overlap=200).chunk_text(
            content, boundaries
        )

        assert len(chunks) == 2
        assert {c.page_number for c in chunks} == {1, 3}

    async def test_offsets_tightened_around_page_whitespace(self):
        """Leading/trailing page whitespace is stripped and offsets adjusted."""
        pages = ["  Leading and trailing.  ", "Normal page."]
        content, boundaries = _make_doc(pages)

        chunks = await PageAwareChunker(chunk_size=2048, overlap=200).chunk_text(
            content, boundaries
        )

        first = chunks[0]
        assert first.text == "Leading and trailing."
        assert content[first.start_offset : first.end_offset] == first.text

    async def test_no_page_boundaries_falls_back_to_char_chunking(self):
        """Without page boundaries, behaves like the char-based chunker."""
        content = "This is sentence one. " * 40

        pa_chunks = await PageAwareChunker(chunk_size=100, overlap=20).chunk_text(
            content, []
        )
        char_chunks = await DocumentChunker(chunk_size=100, overlap=20).chunk_text(
            content
        )

        assert len(pa_chunks) > 1
        # Same chunk boundaries as the char-based path, and no page numbers.
        assert [(c.text, c.start_offset) for c in pa_chunks] == [
            (c.text, c.start_offset) for c in char_chunks
        ]
        assert all(c.page_number is None for c in pa_chunks)

    async def test_all_blank_pages_returns_empty_list(self):
        """Non-empty content whose every page is blank yields no chunks.

        Matches DocumentChunker, which also returns [] for whitespace-only
        non-empty content — the downstream pipeline handles an empty chunk
        list identically for both chunkers.
        """
        pages = ["   ", "\n\n", "\t"]
        content, boundaries = _make_doc(pages)

        chunks = await PageAwareChunker().chunk_text(content, boundaries)

        assert chunks == []
        # Parity: the char-based chunker behaves the same for blank content.
        assert await DocumentChunker().chunk_text(content) == []

    async def test_empty_content_returns_single_empty_chunk(self):
        """Empty content returns one empty chunk regardless of boundaries."""
        chunks = await PageAwareChunker().chunk_text(
            "", [{"page": 1, "start_offset": 0, "end_offset": 0}]
        )
        assert len(chunks) == 1
        assert chunks[0].text == ""
        assert chunks[0].start_offset == 0
        assert chunks[0].end_offset == 0

    async def test_page_end_equals_page_number_without_packing(self):
        """Single-page chunks carry page_end == page_number (citation range)."""
        content, boundaries = _make_doc(["One.", "Two.", "Three."])

        chunks = await PageAwareChunker(chunk_size=2048, overlap=200).chunk_text(
            content, boundaries
        )

        assert len(chunks) == 3
        for chunk in chunks:
            assert chunk.page_end == chunk.page_number

    def test_pack_pages_off_by_default(self):
        """Packing is opt-in; the default keeps one-chunk-per-page."""
        assert PageAwareChunker().pack_pages is False


class TestPageAwarePacking:
    """Test suite for greedy page-packing (Deck #636)."""

    async def test_merges_sub_budget_pages_into_one_chunk(self):
        """Consecutive sub-budget pages merge into a single page-range chunk."""
        content, boundaries = _make_doc(["AAAA", "BBBB", "CCCC"])

        chunks = await PageAwareChunker(
            chunk_size=2048, overlap=200, pack_pages=True
        ).chunk_text(content, boundaries)

        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.text == "AAAABBBB" + "CCCC"
        assert chunk.page_number == 1  # first page
        assert chunk.page_end == 3  # last page — citation range preserved
        assert content[chunk.start_offset : chunk.end_offset] == chunk.text

    async def test_packing_respects_the_budget(self):
        """A page that would overflow the budget starts a fresh chunk."""
        content, boundaries = _make_doc(["AAAAAAAA", "BBBBBBBB", "CCCCCCCC"])  # 8 each

        chunks = await PageAwareChunker(
            chunk_size=20, overlap=5, pack_pages=True
        ).chunk_text(content, boundaries)

        # pages 1+2 (16) fit; adding page 3 (24) overflows -> new chunk.
        assert len(chunks) == 2
        assert (chunks[0].page_number, chunks[0].page_end) == (1, 2)
        assert (chunks[1].page_number, chunks[1].page_end) == (3, 3)
        for chunk in chunks:
            assert len(chunk.text) <= 20
            assert content[chunk.start_offset : chunk.end_offset] == chunk.text

    async def test_oversized_page_flushes_pack_and_splits_within_page(self):
        """An oversized page can't join a pack: flush, then split within-page."""
        content, boundaries = _make_doc(["AAAA", "word " * 200, "BBBB"])

        chunks = await PageAwareChunker(
            chunk_size=200, overlap=20, pack_pages=True
        ).chunk_text(content, boundaries)

        per_page: dict[int, int] = {}
        for chunk in chunks:
            page = chunk.page_number
            assert page is not None
            per_page[page] = per_page.get(page, 0) + 1
            assert chunk.page_end == chunk.page_number  # single-page sub-chunks
        assert per_page[1] == 1  # small page, its own chunk
        assert per_page[2] > 1  # oversized page split within the page
        assert per_page[3] == 1
        for chunk in chunks:
            assert content[chunk.start_offset : chunk.end_offset] == chunk.text

    async def test_no_chunk_exceeds_budget(self):
        """Across a realistic mix, no packed chunk exceeds chunk_size."""
        pages = [f"Page {i} " + "x" * (i * 30) for i in range(1, 12)]
        content, boundaries = _make_doc(pages)

        chunks = await PageAwareChunker(
            chunk_size=200, overlap=20, pack_pages=True
        ).chunk_text(content, boundaries)

        for chunk in chunks:
            page_start = chunk.page_number
            page_last = chunk.page_end
            assert page_start is not None and page_last is not None
            assert len(chunk.text) <= 200
            assert content[chunk.start_offset : chunk.end_offset] == chunk.text
            assert page_start <= page_last

    async def test_blank_interior_page_kept_in_contiguous_pack(self):
        """A blank page interior to a pack contributes no vector of its own."""
        content, boundaries = _make_doc(["Real one.", "   ", "Real two."])

        chunks = await PageAwareChunker(
            chunk_size=2048, overlap=200, pack_pages=True
        ).chunk_text(content, boundaries)

        assert len(chunks) == 1
        assert "Real one." in chunks[0].text
        assert "Real two." in chunks[0].text
        assert chunks[0].page_number == 1

    async def test_packing_reduces_chunk_count_vs_unpacked(self):
        """The density win: packing yields strictly fewer chunks on lean pages."""
        pages = ["Lean page." for _ in range(10)]  # 10 near-empty pages
        content, boundaries = _make_doc(pages)

        unpacked = await PageAwareChunker(chunk_size=2048, pack_pages=False).chunk_text(
            content, boundaries
        )
        packed = await PageAwareChunker(chunk_size=2048, pack_pages=True).chunk_text(
            content, boundaries
        )

        assert len(unpacked) == 10  # one vector per lean page (the inflator)
        assert len(packed) < len(unpacked)  # merged into far fewer vectors
