"""Context expansion for search results.

Provides utilities to expand matched chunks with surrounding context and
position markers for better visualization and understanding of search results.
"""

import logging
from dataclasses import dataclass

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)


async def _get_file_path_from_qdrant(
    user_id: str, file_id: int, chunk_start: int, chunk_end: int
) -> str | None:
    """Resolve file_id to file_path by querying Qdrant payload.

    Args:
        user_id: User ID who owns the file
        file_id: Numeric file ID
        chunk_start: Character offset where chunk starts
        chunk_end: Character offset where chunk ends

    Returns:
        File path string, or None if not found in Qdrant
    """
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from nextcloud_mcp_server.config import get_settings
        from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

        qdrant_client = await get_qdrant_client()
        settings = get_settings()

        # Query for the specific chunk
        scroll_result = await qdrant_client.scroll(
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_id", match=MatchValue(value=file_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value="file")),
                    FieldCondition(
                        key="chunk_start_offset", match=MatchValue(value=chunk_start)
                    ),
                    FieldCondition(
                        key="chunk_end_offset", match=MatchValue(value=chunk_end)
                    ),
                ]
            ),
            limit=1,
            with_payload=["file_path"],
            with_vectors=False,
        )

        if scroll_result[0]:
            point = scroll_result[0][0]
            file_path = point.payload.get("file_path")
            if file_path:
                logger.debug(f"Resolved file_id {file_id} to file_path {file_path}")
                return str(file_path)

        logger.warning(
            f"Could not find file_path in Qdrant for file_id {file_id}, "
            f"chunk [{chunk_start}:{chunk_end}]"
        )
        return None

    except Exception as e:
        logger.error(f"Error querying Qdrant for file_path: {e}", exc_info=True)
        return None


@dataclass
class ChunkContext:
    """Expanded chunk with surrounding context and position markers.

    Attributes:
        chunk_text: The matched chunk text
        before_context: Text before the chunk (up to context_chars)
        after_context: Text after the chunk (up to context_chars)
        chunk_start_offset: Character position where chunk starts in document
        chunk_end_offset: Character position where chunk ends in document
        page_number: Page number for PDFs (None for other doc types)
        chunk_index: Zero-based chunk index (N in "chunk N of M")
        total_chunks: Total number of chunks in document
        marked_text: Full text with position markers around the chunk
        has_before_truncation: True if before_context was truncated
        has_after_truncation: True if after_context was truncated
    """

    chunk_text: str
    before_context: str
    after_context: str
    chunk_start_offset: int
    chunk_end_offset: int
    page_number: int | None
    chunk_index: int
    total_chunks: int
    marked_text: str
    has_before_truncation: bool
    has_after_truncation: bool


async def get_chunk_with_context(
    nc_client: NextcloudClient,
    user_id: str,
    doc_id: str | int,
    doc_type: str,
    chunk_start: int,
    chunk_end: int,
    page_number: int | None = None,
    chunk_index: int = 0,
    total_chunks: int = 1,
    context_chars: int = 300,
) -> ChunkContext | None:
    """Fetch chunk with surrounding context from original document.

    Retrieves the full document text and expands the matched chunk to include
    surrounding context for better understanding. Inserts position markers
    around the chunk for visualization.

    Args:
        nc_client: Authenticated Nextcloud client
        user_id: User ID who owns the document
        doc_id: Document ID (int for notes/files)
        doc_type: Type of document ("note", "file", etc.)
        chunk_start: Character offset where chunk starts
        chunk_end: Character offset where chunk ends
        page_number: Optional page number for PDFs
        chunk_index: Zero-based chunk index in document
        total_chunks: Total number of chunks in document
        context_chars: Number of characters to include before/after chunk

    Returns:
        ChunkContext with expanded context and markers, or None if document
        cannot be retrieved
    """
    # For files, retrieve file_path from Qdrant payload
    resolved_doc_id = doc_id
    if doc_type == "file" and isinstance(doc_id, int):
        file_path = await _get_file_path_from_qdrant(
            user_id, doc_id, chunk_start, chunk_end
        )
        if not file_path:
            logger.warning(
                f"Could not resolve file_id {doc_id} to file_path from Qdrant"
            )
            return None
        resolved_doc_id = file_path
        logger.debug(f"Resolved file_id {doc_id} to file_path {file_path}")

    # Fetch full document text
    full_text = await _fetch_document_text(nc_client, resolved_doc_id, doc_type)
    if full_text is None:
        logger.warning(
            f"Could not fetch document text for {doc_type} {doc_id}, "
            "skipping context expansion"
        )
        return None

    # Validate offsets
    if chunk_start < 0 or chunk_end > len(full_text) or chunk_start >= chunk_end:
        logger.warning(
            f"Invalid chunk offsets for {doc_type} {doc_id}: "
            f"start={chunk_start}, end={chunk_end}, doc_len={len(full_text)}"
        )
        return None

    # Extract chunk text
    chunk_text = full_text[chunk_start:chunk_end]

    # Calculate context boundaries
    context_start = max(0, chunk_start - context_chars)
    context_end = min(len(full_text), chunk_end + context_chars)

    # Extract context
    before_context = full_text[context_start:chunk_start]
    after_context = full_text[chunk_end:context_end]

    # Check for truncation
    has_before_truncation = context_start > 0
    has_after_truncation = context_end < len(full_text)

    # Create marked text with position markers
    marked_text = _insert_position_markers(
        before_context=before_context,
        chunk_text=chunk_text,
        after_context=after_context,
        page_number=page_number,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        has_before_truncation=has_before_truncation,
        has_after_truncation=has_after_truncation,
    )

    return ChunkContext(
        chunk_text=chunk_text,
        before_context=before_context,
        after_context=after_context,
        chunk_start_offset=chunk_start,
        chunk_end_offset=chunk_end,
        page_number=page_number,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        marked_text=marked_text,
        has_before_truncation=has_before_truncation,
        has_after_truncation=has_after_truncation,
    )


async def _fetch_document_text(
    nc_client: NextcloudClient, doc_id: str | int, doc_type: str
) -> str | None:
    """Fetch full text content of a document.

    Args:
        nc_client: Authenticated Nextcloud client
        doc_id: Document ID (note ID or file path)
        doc_type: Type of document ("note", "file", etc.)

    Returns:
        Full document text, or None if document cannot be retrieved
    """
    try:
        if doc_type == "note":
            # Fetch note by ID
            note = await nc_client.notes.get_note(note_id=int(doc_id))
            # Reconstruct full content as indexed: title + "\n\n" + content
            # This ensures chunk offsets align with indexed content structure
            title = note.get("title", "")
            content = note.get("content", "")
            return f"{title}\n\n{content}"
        elif doc_type == "file":
            # Fetch file content via WebDAV
            try:
                file_path = str(doc_id)
                file_content, content_type = await nc_client.webdav.read_file(file_path)

                # Check if it's a PDF (by content type or file extension)
                is_pdf = (
                    content_type and "pdf" in content_type.lower()
                ) or file_path.lower().endswith(".pdf")

                if is_pdf:
                    # Extract text from PDF using PyMuPDF
                    # IMPORTANT: Use pymupdf4llm.to_markdown() to match indexing extraction
                    # This ensures character offsets align between indexed chunks and retrieval
                    import pymupdf
                    import pymupdf4llm

                    logger.debug(f"Extracting text from PDF: {file_path}")
                    pdf_doc = pymupdf.open(stream=file_content, filetype="pdf")
                    text_parts = []

                    # Extract each page as markdown (same as indexing)
                    for page_num in range(pdf_doc.page_count):
                        page_md = pymupdf4llm.to_markdown(
                            pdf_doc,
                            pages=[page_num],
                            write_images=False,  # Don't need images for context
                            page_chunks=False,
                        )
                        text_parts.append(page_md)

                    pdf_doc.close()

                    # Join pages (no separator - matches indexing)
                    full_text = "".join(text_parts)
                    logger.debug(
                        f"Extracted {len(full_text)} characters from "
                        f"{pdf_doc.page_count} pages in {file_path}"
                    )
                    return full_text
                else:
                    # Assume it's a text file, decode to string
                    logger.debug(f"Decoding text file: {file_path}")
                    return file_content.decode("utf-8", errors="replace")
            except Exception as e:
                logger.error(
                    f"Error fetching file content for {doc_id}: {e}", exc_info=True
                )
                return None
        else:
            logger.warning(f"Unsupported doc_type for context expansion: {doc_type}")
            return None
    except Exception as e:
        logger.error(f"Error fetching document {doc_type} {doc_id}: {e}", exc_info=True)
        return None


def _insert_position_markers(
    before_context: str,
    chunk_text: str,
    after_context: str,
    page_number: int | None,
    chunk_index: int,
    total_chunks: int,
    has_before_truncation: bool,
    has_after_truncation: bool,
) -> str:
    """Insert position markers around matched chunk.

    Creates markdown-formatted text with visual markers indicating chunk
    boundaries and metadata.

    Args:
        before_context: Text before chunk
        chunk_text: The matched chunk
        after_context: Text after chunk
        page_number: Optional page number
        chunk_index: Zero-based chunk index
        total_chunks: Total chunks in document
        has_before_truncation: Whether before_context is truncated
        has_after_truncation: Whether after_context is truncated

    Returns:
        Formatted text with position markers
    """
    # Build position metadata
    position_parts = []
    if page_number is not None:
        position_parts.append(f"Page {page_number}")
    position_parts.append(f"Chunk {chunk_index + 1} of {total_chunks}")
    position_metadata = ", ".join(position_parts)

    # Build marked text
    parts = []

    # Add truncation indicator for before context
    if has_before_truncation:
        parts.append("**[...]**\n\n")

    # Add before context if present
    if before_context:
        parts.append(before_context)

    # Add chunk start marker
    parts.append(f"\n\n🔍 **MATCHED CHUNK START** ({position_metadata})\n\n")

    # Add chunk text
    parts.append(chunk_text)

    # Add chunk end marker
    parts.append("\n\n🔍 **MATCHED CHUNK END**\n\n")

    # Add after context if present
    if after_context:
        parts.append(after_context)

    # Add truncation indicator for after context
    if has_after_truncation:
        parts.append("\n\n**[...]**")

    return "".join(parts)
