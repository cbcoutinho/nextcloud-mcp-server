"""Document chunking for large texts using LangChain text splitters."""

import logging
from dataclasses import dataclass

from langchain_text_splitters import MarkdownTextSplitter

logger = logging.getLogger(__name__)


@dataclass
class ChunkWithPosition:
    """A text chunk with its character position in the original document."""

    text: str
    start_offset: int  # Character position where chunk starts
    end_offset: int  # Character position where chunk ends (exclusive)


class DocumentChunker:
    """Chunk large documents for optimal embedding using LangChain text splitters.

    Uses MarkdownTextSplitter which is optimized for Markdown content like
    Nextcloud Notes. Respects markdown structure (headers, code blocks, lists)
    while maintaining semantic boundaries.
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

        # Initialize LangChain MarkdownTextSplitter
        # Optimized for Markdown content with special handling for:
        # - Headers (# ## ###)
        # - Code blocks (``` ```)
        # - Lists (- * 1.)
        # - Horizontal rules (---)
        # - Paragraphs and sentences
        # This preserves both markdown structure and semantic boundaries
        self.splitter = MarkdownTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            add_start_index=True,  # Enable position tracking
            strip_whitespace=True,
        )

    def chunk_text(self, content: str) -> list[ChunkWithPosition]:
        """
        Split text into overlapping chunks with position tracking.

        Uses LangChain's MarkdownTextSplitter to create chunks that respect
        both markdown structure and semantic boundaries. Optimized for Nextcloud
        Notes content with special handling for headers, code blocks, lists, etc.
        Preserves character positions for each chunk to enable precise document
        retrieval.

        Args:
            content: Markdown text content to chunk

        Returns:
            List of chunks with their character positions in the original content
        """
        # Handle empty content - return single empty chunk for backward compatibility
        if not content:
            return [ChunkWithPosition(text="", start_offset=0, end_offset=0)]

        # Use LangChain to create documents with position tracking
        docs = self.splitter.create_documents([content])

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
            f"Chunked document into {len(chunks)} chunks "
            f"(chunk_size={self.chunk_size}, overlap={self.overlap})"
        )
        return chunks
