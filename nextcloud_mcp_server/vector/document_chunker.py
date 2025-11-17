"""Document chunking for large texts."""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ChunkWithPosition:
    """A text chunk with its character position in the original document."""

    text: str
    start_offset: int  # Character position where chunk starts
    end_offset: int  # Character position where chunk ends (exclusive)


class DocumentChunker:
    """Chunk large documents for optimal embedding."""

    def __init__(self, chunk_size: int = 512, overlap: int = 50):
        """
        Initialize document chunker.

        Args:
            chunk_size: Number of words per chunk (default: 512)
            overlap: Number of overlapping words between chunks (default: 50)
        """
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk_text(self, content: str) -> list[ChunkWithPosition]:
        """
        Split text into overlapping chunks with position tracking.

        Uses simple word-based chunking with configurable overlap to preserve
        context across chunk boundaries. Tracks character positions for each chunk.

        Args:
            content: Text content to chunk

        Returns:
            List of chunks with their character positions in the original content
        """
        # Use regex to find all words and their positions
        # This preserves the original spacing and allows accurate position tracking
        word_pattern = re.compile(r"\S+")
        word_matches = list(word_pattern.finditer(content))

        if len(word_matches) <= self.chunk_size:
            # Single chunk - use entire content
            return [
                ChunkWithPosition(text=content, start_offset=0, end_offset=len(content))
            ]

        chunks = []
        start_idx = 0

        while start_idx < len(word_matches):
            end_idx = min(start_idx + self.chunk_size, len(word_matches))

            # Get the first and last word positions
            first_word = word_matches[start_idx]
            last_word = word_matches[end_idx - 1]

            # Extract chunk using character positions
            start_offset = first_word.start()
            end_offset = last_word.end()
            chunk_text = content[start_offset:end_offset]

            chunks.append(
                ChunkWithPosition(
                    text=chunk_text, start_offset=start_offset, end_offset=end_offset
                )
            )

            # If we've reached the end, break
            if end_idx >= len(word_matches):
                break

            # Move to next chunk with overlap
            next_start_idx = end_idx - self.overlap

            # Safety check: ensure we're making forward progress
            # If we're not advancing (overlap >= chunk processed), break to prevent infinite loop
            if next_start_idx <= start_idx:
                break

            start_idx = next_start_idx

        logger.debug(
            f"Chunked document into {len(chunks)} chunks ({len(word_matches)} words)"
        )
        return chunks
