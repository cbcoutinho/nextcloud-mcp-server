"""Document chunking for large texts."""

import logging

logger = logging.getLogger(__name__)


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

    def chunk_text(self, content: str) -> list[str]:
        """
        Split text into overlapping chunks.

        Uses simple word-based chunking with configurable overlap to preserve
        context across chunk boundaries.

        Args:
            content: Text content to chunk

        Returns:
            List of text chunks (may be single item if content is small)
        """
        # Simple word-based chunking
        words = content.split()

        if len(words) <= self.chunk_size:
            return [content]

        chunks = []
        start = 0

        while start < len(words):
            end = start + self.chunk_size
            chunk_words = words[start:end]
            chunks.append(" ".join(chunk_words))
            start = end - self.overlap

        logger.debug(f"Chunked document into {len(chunks)} chunks ({len(words)} words)")
        return chunks
