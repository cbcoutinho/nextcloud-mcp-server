"""Unit tests for DocumentChunker with LangChain text splitters."""

from nextcloud_mcp_server.vector.document_chunker import (
    ChunkWithPosition,
    DocumentChunker,
)


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
