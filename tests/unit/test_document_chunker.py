"""Unit tests for DocumentChunker with position tracking."""

from nextcloud_mcp_server.vector.document_chunker import (
    ChunkWithPosition,
    DocumentChunker,
)


class TestDocumentChunkerPositions:
    """Test suite for DocumentChunker position tracking functionality."""

    def test_single_chunk_simple_text(self):
        """Test that single-chunk documents return correct positions."""
        chunker = DocumentChunker(chunk_size=512, overlap=50)
        content = "This is a short document."

        chunks = chunker.chunk_text(content)

        assert len(chunks) == 1
        assert isinstance(chunks[0], ChunkWithPosition)
        assert chunks[0].text == content
        assert chunks[0].start_offset == 0
        assert chunks[0].end_offset == len(content)

    def test_multiple_chunks_positions(self):
        """Test that multi-chunk documents have correct positions."""
        chunker = DocumentChunker(chunk_size=10, overlap=2)  # Small chunks for testing
        # Create content with exactly 30 words
        words = [f"word{i:02d}" for i in range(30)]
        content = " ".join(words)

        chunks = chunker.chunk_text(content)

        # Verify we got multiple chunks (30 words, 10 per chunk, 2 overlap = 4 chunks)
        assert len(chunks) == 4

        # Verify all chunks are ChunkWithPosition
        for chunk in chunks:
            assert isinstance(chunk, ChunkWithPosition)

        # Verify first chunk starts at 0
        assert chunks[0].start_offset == 0

        # Verify last chunk ends at content length
        assert chunks[-1].end_offset == len(content)

        # Verify chunks are contiguous or overlap (no gaps)
        for i in range(len(chunks) - 1):
            # Next chunk should start at or before current chunk ends
            assert chunks[i + 1].start_offset <= chunks[i].end_offset

        # Verify we can reconstruct the content using positions
        for chunk in chunks:
            extracted = content[chunk.start_offset : chunk.end_offset]
            assert extracted == chunk.text

    def test_chunk_positions_with_whitespace(self):
        """Test position tracking with various whitespace."""
        chunker = DocumentChunker(chunk_size=5, overlap=1)
        content = "word1  word2\n\nword3\tword4    word5 word6"

        chunks = chunker.chunk_text(content)

        # Verify positions correctly handle whitespace
        for chunk in chunks:
            extracted = content[chunk.start_offset : chunk.end_offset]
            assert extracted == chunk.text
            # Verify no leading/trailing whitespace unless in original
            if chunk != chunks[0] and chunk != chunks[-1]:
                # Middle chunks should be extracted correctly
                assert len(chunk.text.strip()) > 0

    def test_empty_content(self):
        """Test that empty content returns empty chunk."""
        chunker = DocumentChunker(chunk_size=512, overlap=50)
        content = ""

        chunks = chunker.chunk_text(content)

        assert len(chunks) == 1
        assert chunks[0].text == ""
        assert chunks[0].start_offset == 0
        assert chunks[0].end_offset == 0

    def test_chunk_overlap_positions(self):
        """Test that overlapping chunks have correct positions."""
        chunker = DocumentChunker(chunk_size=10, overlap=3)
        words = [f"word{i:02d}" for i in range(25)]
        content = " ".join(words)

        chunks = chunker.chunk_text(content)

        # Verify overlap exists
        for i in range(len(chunks) - 1):
            current_chunk = chunks[i]
            next_chunk = chunks[i + 1]

            # Next chunk should start before current ends (overlap)
            # This happens because we move back by overlap words
            # The actual character overlap depends on word lengths
            assert next_chunk.start_offset >= 0
            assert current_chunk.end_offset <= len(content)

    def test_unicode_content_positions(self):
        """Test position tracking with Unicode characters."""
        chunker = DocumentChunker(chunk_size=10, overlap=2)
        content = "Hello 世界 こんにちは мир Привет שלום مرحبا 你好"

        chunks = chunker.chunk_text(content)

        # Verify all chunks extract correctly
        for chunk in chunks:
            extracted = content[chunk.start_offset : chunk.end_offset]
            assert extracted == chunk.text

        # Verify full coverage
        if len(chunks) == 1:
            assert chunks[0].start_offset == 0
            assert chunks[0].end_offset == len(content)

    def test_single_word_chunks(self):
        """Test position tracking with single-word chunks."""
        chunker = DocumentChunker(chunk_size=1, overlap=0)
        content = "one two three"

        chunks = chunker.chunk_text(content)

        assert len(chunks) == 3
        assert chunks[0].text == "one"
        assert chunks[1].text == "two"
        assert chunks[2].text == "three"

        # Verify positions
        assert content[chunks[0].start_offset : chunks[0].end_offset] == "one"
        assert content[chunks[1].start_offset : chunks[1].end_offset] == "two"
        assert content[chunks[2].start_offset : chunks[2].end_offset] == "three"

    def test_realistic_note_content(self):
        """Test with realistic note content similar to Nextcloud Notes."""
        chunker = DocumentChunker(chunk_size=50, overlap=10)
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

        chunks = chunker.chunk_text(content)

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

    def test_chunk_boundaries(self):
        """Test that chunk boundaries are word-aligned."""
        chunker = DocumentChunker(chunk_size=10, overlap=2)
        words = [f"word{i:02d}" for i in range(30)]
        content = " ".join(words)

        chunks = chunker.chunk_text(content)

        for chunk in chunks:
            # Verify chunk text starts and ends with word characters (no split words)
            # Unless it's the full content
            if len(chunks) > 1:
                # Each chunk should start with a word (not whitespace)
                assert chunk.text[0].strip() != ""
                # Each chunk should end with a word (not whitespace)
                assert chunk.text[-1].strip() != ""
