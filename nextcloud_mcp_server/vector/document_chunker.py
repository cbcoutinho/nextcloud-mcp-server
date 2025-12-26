"""Document chunking for large texts using LangChain text splitters."""

import logging
from dataclasses import dataclass

import anyio
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)


@dataclass
class ChunkWithPosition:
    """A text chunk with its character position in the original document."""

    text: str
    start_offset: int  # Character position where chunk starts
    end_offset: int  # Character position where chunk ends (exclusive)
    page_number: int | None = None  # Page number for PDF chunks (optional)
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
            f"Chunked document into {len(chunks)} chunks "
            f"(chunk_size={self.chunk_size}, overlap={self.overlap})"
        )
        return chunks
