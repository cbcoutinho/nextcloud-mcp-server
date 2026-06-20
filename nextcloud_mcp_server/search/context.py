"""Context expansion for search results.

Provides utilities to expand matched chunks with surrounding context and
position markers for better visualization and understanding of search results.
"""

import logging
from dataclasses import dataclass
from typing import cast

from httpx import HTTPStatusError
from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.models.deck import DeckCard
from nextcloud_mcp_server.search.access_filter import build_ownership_filter
from nextcloud_mcp_server.utils.validation import is_valid_nextcloud_doc_id
from nextcloud_mcp_server.vector.html_processor import html_to_markdown
from nextcloud_mcp_server.vector.mail_content import build_mail_content
from nextcloud_mcp_server.vector.placeholder import get_placeholder_filter
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


async def _get_chunk_from_qdrant(
    user_id: str,
    doc_id: str,
    doc_type: str,
    chunk_start: int,
    chunk_end: int,
    accessible_owners: list[str] | None = None,
) -> str | None:
    """Retrieve full chunk text from Qdrant payload.

    This avoids re-fetching and re-parsing documents by using the cached
    chunk content already stored in Qdrant.

    Args:
        user_id: Querying user.
        doc_id: Document ID
        doc_type: Document type (e.g., "note", "file")
        chunk_start: Character offset where chunk starts
        chunk_end: Character offset where chunk ends
        accessible_owners: Owner UIDs the caller may read (self + share senders).
            When None, the lookup is self-only. Callers must only pass an
            expanded set after confirming the caller can access the document
            (see ``get_chunk_with_context``) — the filter is owner-level.

    Returns:
        Full chunk text from Qdrant excerpt field, or None if not found
    """
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()

        # Query for the specific chunk
        scroll_result = await qdrant_client.scroll(
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    build_ownership_filter(user_id, accessible_owners),
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
                    FieldCondition(
                        key="chunk_start_offset", match=MatchValue(value=chunk_start)
                    ),
                    FieldCondition(
                        key="chunk_end_offset", match=MatchValue(value=chunk_end)
                    ),
                ]
            ),
            limit=1,
            with_payload=["excerpt"],
            with_vectors=False,
        )

        if scroll_result[0]:
            point = scroll_result[0][0]
            excerpt = point.payload.get("excerpt")
            if excerpt:
                logger.debug(
                    "Retrieved chunk from Qdrant for %s %s: %s chars",
                    doc_type,
                    doc_id,
                    len(excerpt),
                )
                return str(excerpt)

        logger.debug(
            "Chunk not found in Qdrant for %s %s, chunk [%s:%s]. Will fall back to document fetch.",
            doc_type,
            doc_id,
            chunk_start,
            chunk_end,
        )
        return None

    except Exception as e:
        logger.error(
            "Error querying Qdrant for chunk: %s. Falling back to document fetch.",
            e,
            exc_info=True,
        )
        return None


async def _get_chunk_by_index_from_qdrant(
    user_id: str,
    doc_id: str,
    doc_type: str,
    chunk_index: int,
    accessible_owners: list[str] | None = None,
) -> str | None:
    """Retrieve chunk text by chunk_index from Qdrant payload.

    Used to fetch adjacent chunks for context expansion.

    Args:
        user_id: Querying user.
        doc_id: Document ID
        doc_type: Document type (e.g., "note", "file")
        chunk_index: Zero-based chunk index in document
        accessible_owners: Owner UIDs the caller may read; None ⇒ self-only.
            Only pass an expanded set after a per-document access check (see
            ``get_chunk_with_context``).

    Returns:
        Full chunk text from Qdrant excerpt field, or None if not found
    """
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()

        # Query for chunk by index
        scroll_result = await qdrant_client.scroll(
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    build_ownership_filter(user_id, accessible_owners),
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
                    FieldCondition(
                        key="chunk_index", match=MatchValue(value=chunk_index)
                    ),
                ]
            ),
            limit=1,
            with_payload=["excerpt"],
            with_vectors=False,
        )

        if scroll_result[0]:
            point = scroll_result[0][0]
            excerpt = point.payload.get("excerpt")
            if excerpt:
                logger.debug(
                    "Retrieved adjacent chunk %s from Qdrant for %s %s: %s chars",
                    chunk_index,
                    doc_type,
                    doc_id,
                    len(excerpt),
                )
                return str(excerpt)

        return None

    except Exception as e:
        logger.debug(
            "Could not retrieve adjacent chunk %s for %s %s: %s",
            chunk_index,
            doc_type,
            doc_id,
            e,
        )
        return None


async def _get_deck_metadata_from_qdrant(
    user_id: str, card_id: str
) -> dict[str, int] | None:
    """Retrieve board_id and stack_id for a deck card from Qdrant payload.

    Args:
        user_id: User ID who owns the card
        card_id: Card ID

    Returns:
        Dictionary with board_id and stack_id, or None if not found
    """
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()

        # Query for any chunk of this card (we just need metadata).
        # Intentionally self-only (raw user_id, not build_ownership_filter):
        # deck cards are a documented cross-user gap — the Deck API is per-user,
        # so cross-user deck context can't be fetched with the caller's
        # credentials anyway (see the doc_type=="file"-only gate in
        # get_chunk_with_context). Every other internal Qdrant lookup here is
        # ACL-aware; this one is the deliberate exception.
        scroll_result = await qdrant_client.scroll(
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_id", match=MatchValue(value=card_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value="deck_card")),
                ]
            ),
            limit=1,
            with_payload=["board_id", "stack_id"],
            with_vectors=False,
        )

        if scroll_result[0]:
            point = scroll_result[0][0]
            board_id = point.payload.get("board_id")
            stack_id = point.payload.get("stack_id")
            if board_id is not None and stack_id is not None:
                logger.debug(
                    "Retrieved deck metadata for card %s: board_id=%s, stack_id=%s",
                    card_id,
                    board_id,
                    stack_id,
                )
                return {"board_id": int(board_id), "stack_id": int(stack_id)}

        logger.debug(
            "Could not find deck metadata in Qdrant for card %s (might be legacy data without board_id/stack_id)",
            card_id,
        )
        return None

    except Exception as e:
        logger.debug("Error querying Qdrant for deck metadata: %s", e)
        return None


async def get_chunk_bbox_and_page_from_qdrant(
    user_id: str,
    doc_id: str,
    chunk_index: int | None,
    chunk_start: int,
    chunk_end: int,
    accessible_owners: list[str] | None = None,
) -> tuple[list | None, int | None]:
    """Fetch chunk_bbox and page_number for a chunk from Qdrant payload.

    Prefers chunk_index for the lookup (always indexed); falls back to
    (chunk_start_offset, chunk_end_offset) when chunk_index is not provided
    — this is the legacy path for clients pre-cbcoutinho/astrolabe#75. The
    fallback may 400 in Qdrant Cloud strict mode because those offset fields
    aren't indexed there; that's logged as a warning and (None, None) is
    returned so callers degrade gracefully.

    Args:
        user_id: User ID who owns the document
        doc_id: Document ID — always a string. Producers stringify their
            native ID before writing to Qdrant so the keyword payload
            index on ``doc_id`` matches every point regardless of source
            doc_type. An ``int`` filter against the str-indexed payload
            would silently match zero points.
        chunk_index: Zero-based chunk index, or None to use offset fallback
        chunk_start: Character offset where chunk starts (used when
            chunk_index is None)
        chunk_end: Character offset where chunk ends (used when chunk_index
            is None)

    Returns:
        Tuple of (chunk_bbox, page_number); either field may be None
        independently if absent from the payload, or both may be None on
        miss/error.
    """
    try:
        settings = get_settings()
        qdrant_client = await get_qdrant_client()

        if chunk_index is not None:
            points_response = await qdrant_client.scroll(
                collection_name=settings.get_collection_name(),
                scroll_filter=Filter(
                    must=[
                        get_placeholder_filter(),
                        FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                        build_ownership_filter(user_id, accessible_owners),
                        FieldCondition(
                            key="chunk_index", match=MatchValue(value=chunk_index)
                        ),
                    ]
                ),
                limit=1,
                with_vectors=False,
                with_payload=["chunk_bbox", "page_number"],
            )
        else:
            points_response = await qdrant_client.scroll(
                collection_name=settings.get_collection_name(),
                scroll_filter=Filter(
                    must=[
                        get_placeholder_filter(),
                        FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                        build_ownership_filter(user_id, accessible_owners),
                        FieldCondition(
                            key="chunk_start_offset",
                            match=MatchValue(value=chunk_start),
                        ),
                        FieldCondition(
                            key="chunk_end_offset",
                            match=MatchValue(value=chunk_end),
                        ),
                    ]
                ),
                limit=1,
                with_vectors=False,
                with_payload=["chunk_bbox", "page_number"],
            )

        points = points_response[0]
        if not points or not points[0].payload:
            return None, None

        payload = points[0].payload
        chunk_bbox = payload.get("chunk_bbox")
        page_number = payload.get("page_number")
        if chunk_bbox:
            logger.info(
                "Found chunk bbox: page=%s, rects=%d", page_number, len(chunk_bbox)
            )
        return chunk_bbox, page_number

    except Exception as e:
        logger.warning("Failed to fetch chunk bbox: %s", e)
        return None, None


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
        chunk_index: Zero-based chunk index (N in "chunk N of M"). None when
            the caller didn't supply chunk_index and we couldn't determine it
            from the lookup path — distinguishes "unknown position" from
            "actually chunk 0".
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
    chunk_index: int | None
    total_chunks: int
    marked_text: str
    has_before_truncation: bool
    has_after_truncation: bool


async def get_chunk_with_context(
    nc_client: NextcloudClient,
    user_id: str,
    doc_id: str,
    doc_type: str,
    chunk_start: int,
    chunk_end: int,
    page_number: int | None = None,
    chunk_index: int | None = None,
    total_chunks: int = 1,
    context_chars: int = 300,
    accessible_owners: list[str] | None = None,
) -> ChunkContext | None:
    """Fetch chunk with surrounding context.

    First tries to retrieve the chunk from Qdrant (fast, cached). If that fails
    (e.g., legacy data with truncated excerpts), falls back to fetching and
    parsing the full document (slower, for PDFs especially).

    Args:
        nc_client: Authenticated Nextcloud client
        user_id: Querying user.
        doc_id: Document ID (str — keyword-indexed in Qdrant payload)
        doc_type: Type of document ("note", "file", etc.)
        chunk_start: Character offset where chunk starts
        chunk_end: Character offset where chunk ends
        page_number: Optional page number for PDFs
        chunk_index: Zero-based chunk index in document. When provided, used as
            the primary Qdrant lookup key (uses the always-indexed chunk_index
            field). When None, falls back to the (chunk_start, chunk_end) lookup.
        total_chunks: Total number of chunks in document
        context_chars: Number of characters to include before/after chunk
        accessible_owners: Owner UIDs the caller may read (self + share senders).
            Used to support cross-user context for SHARED FILES only, and only
            after a per-file access check (see ``lookup_owners`` below). For
            non-file types the lookup stays self-only.

    Returns:
        ChunkContext with expanded context and markers, or None if document
        cannot be retrieved
    """
    # doc_id is keyword-indexed in Qdrant as str — pass through verbatim
    # (no int coercion; producers always stringify on write).

    # Determine the ownership scope for the Qdrant cached-chunk lookups.
    #
    # ``accessible_owners`` is OWNER-level (every owner who shared anything with
    # the caller), so widening the lookup to it unconditionally would let a
    # recipient of a single shared file read ANY of that owner's cached chunks
    # by guessing doc_ids. We therefore honour it only for FILES, and only after
    # confirming the caller can access THIS file by id (``file_accessible_by_id``
    # is cross-user-safe: a WebDAV SEARCH over the caller's whole tree incl.
    # mounted shares). For per-user types (note/deck/news) there is no
    # share-mounted by-id access via the caller's credentials, so the lookup
    # stays self-only — cross-user context for those types is a known gap.
    lookup_owners: list[str] | None = None  # None ⇒ self-only
    if doc_type == "file" and accessible_owners:
        try:
            if await nc_client.webdav.file_accessible_by_id(int(doc_id)):
                lookup_owners = accessible_owners
            else:
                # Not owned and not shared with the caller → no access. Return
                # early rather than falling back to a self-only lookup that
                # would also miss (and so the result is the same None, but this
                # is explicit and skips a pointless Qdrant round-trip).
                logger.debug(
                    "File %s not accessible to %s; no cross-user chunk context",
                    doc_id,
                    user_id,
                )
                return None
        except (ValueError, TypeError):
            # Non-numeric doc_id: shouldn't happen (endpoints validate), but
            # degrade to self-only rather than raising.
            logger.warning("Non-numeric file doc_id %r; using self-only scope", doc_id)
        except HTTPStatusError as exc:
            # Transient transport/server error — treat as inconclusive and fall
            # back to self-only so the caller's own files still resolve.
            logger.warning(
                "file_accessible_by_id(%s) failed (%s); using self-only scope",
                doc_id,
                exc,
            )

    # Try to get chunk from Qdrant (fast path).
    # Prefer chunk_index lookup (always-indexed field) when caller supplied it;
    # fall back to (chunk_start, chunk_end) lookup otherwise.
    chunk_text: str | None = None
    if chunk_index is not None:
        chunk_text = await _get_chunk_by_index_from_qdrant(
            user_id, doc_id, doc_type, chunk_index, accessible_owners=lookup_owners
        )
    # When chunk_index is supplied, the indexed lookup is canonical: both the
    # index path and the offset path query the same Qdrant collection, so an
    # indexed miss means the chunk is genuinely absent. Skipping the offset
    # filter avoids a redundant Qdrant round-trip. Legacy data without
    # chunk_index (pre-cbcoutinho/astrolabe#75) still hits the offset path
    # and degrades to a None chunk with a WARNING; that's the same behavior
    # get_chunk_bbox_and_page_from_qdrant already documents.
    skip_offset_lookup = chunk_index is not None
    if chunk_text is None and not skip_offset_lookup:
        chunk_text = await _get_chunk_from_qdrant(
            user_id,
            doc_id,
            doc_type,
            chunk_start,
            chunk_end,
            accessible_owners=lookup_owners,
        )

    if chunk_text:
        logger.info(
            "Retrieved chunk from Qdrant cache for %s %s (avoids document re-fetch/re-parse)",
            doc_type,
            doc_id,
        )

        # Fetch adjacent chunks for context expansion
        # Get chunk overlap from config to remove duplicate text
        settings = get_settings()
        chunk_overlap = settings.document_chunk_overlap

        before_context = ""
        after_context = ""
        has_before_truncation = False
        has_after_truncation = False

        if chunk_index is not None:
            # Fetch previous chunk if not first chunk
            if chunk_index > 0:
                before_chunk = await _get_chunk_by_index_from_qdrant(
                    user_id,
                    doc_id,
                    doc_type,
                    chunk_index - 1,
                    accessible_owners=lookup_owners,
                )
                if before_chunk:
                    # Remove overlap: the last chunk_overlap chars of previous chunk
                    # overlap with the first chunk_overlap chars of current chunk
                    before_context = (
                        before_chunk[:-chunk_overlap]
                        if len(before_chunk) > chunk_overlap
                        else ""
                    )
                    # Truncate if requested context_chars < remaining length
                    if before_context and len(before_context) > context_chars:
                        before_context = before_context[-context_chars:]
                        has_before_truncation = True
                else:
                    # Could not fetch previous chunk, but we're not at start
                    has_before_truncation = True

            # Fetch next chunk if not last chunk
            if chunk_index < total_chunks - 1:
                after_chunk = await _get_chunk_by_index_from_qdrant(
                    user_id,
                    doc_id,
                    doc_type,
                    chunk_index + 1,
                    accessible_owners=lookup_owners,
                )
                if after_chunk:
                    # Remove overlap: the first chunk_overlap chars of next chunk
                    # overlap with the last chunk_overlap chars of current chunk
                    after_context = (
                        after_chunk[chunk_overlap:]
                        if len(after_chunk) > chunk_overlap
                        else ""
                    )
                    # Truncate if requested context_chars < remaining length
                    if after_context and len(after_context) > context_chars:
                        after_context = after_context[:context_chars]
                        has_after_truncation = True
                else:
                    # Could not fetch next chunk, but we're not at end
                    has_after_truncation = True
        else:
            # No chunk_index → can't fetch adjacent chunks via index arithmetic
            # without risking wrong neighbours (a default of 0 would query the
            # chunks at positions -1 and 1 even when the actual chunk is, say,
            # 5/20). Mark both sides as truncated so the caller knows context
            # wasn't expanded.
            has_before_truncation = True
            has_after_truncation = True

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

    # Fallback: Fetch full document and extract chunk with context.
    # For files this path requires downloading and re-parsing the PDF, which
    # routinely exceeds 30s on large documents. Skip it: if the chunk wasn't
    # found by chunk_index OR offsets, re-parsing the PDF won't find it either
    # (the chunk has been removed or re-indexed with different offsets).
    if doc_type == "file":
        logger.warning(
            "Chunk not found in Qdrant for file %s (chunk_index=%s, "
            "offsets=%s-%s); skipping slow PDF re-parse fallback",
            doc_id,
            chunk_index,
            chunk_start,
            chunk_end,
        )
        return None

    logger.info(
        "Falling back to document fetch for %s %s (Qdrant cache miss, possibly legacy data)",
        doc_type,
        doc_id,
    )

    # Fetch full document text (notes, deck cards, news items, etc.)
    full_text = await _fetch_document_text(nc_client, doc_id, doc_type, user_id)
    if full_text is None:
        logger.warning(
            "Could not fetch document text for %s %s, skipping context expansion",
            doc_type,
            doc_id,
        )
        return None

    # Validate offsets
    if chunk_start < 0 or chunk_end > len(full_text) or chunk_start >= chunk_end:
        logger.warning(
            "Invalid chunk offsets for %s %s: start=%s, end=%s, doc_len=%s",
            doc_type,
            doc_id,
            chunk_start,
            chunk_end,
            len(full_text),
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
    nc_client: NextcloudClient, doc_id: str, doc_type: str, user_id: str
) -> str | None:
    """Fetch full text content of a document.

    Note: doc_type=="file" is short-circuited in get_chunk_with_context before
    this function is called (re-parsing PDFs is too slow for the request
    timeout), so no file branch exists here.

    Args:
        nc_client: Authenticated Nextcloud client
        doc_id: Document ID
        doc_type: Type of document ("note", "news_item", "deck_card")

    Returns:
        Full document text, or None if document cannot be retrieved
    """
    try:
        if doc_type == "note":
            # Note IDs are positive ASCII integers (MySQL AUTO_INCREMENT).
            # is_valid_nextcloud_doc_id rejects "0", leading zeros, and Unicode
            # digits that pass str.isdigit(); a malformed payload surfaces in
            # logs rather than getting silently swallowed by `except Exception`.
            if not is_valid_nextcloud_doc_id(doc_id):
                logger.warning(
                    "Expected numeric note doc_id, got %r — skipping document fetch",
                    doc_id,
                )
                return None
            # Fetch note by ID
            note = await nc_client.notes.get_note(note_id=int(doc_id))
            # Reconstruct full content as indexed: title + "\n\n" + content
            # This ensures chunk offsets align with indexed content structure
            title = note.get("title", "")
            content = note.get("content", "")
            return f"{title}\n\n{content}"
        elif doc_type == "news_item":
            # News item IDs are positive ASCII integers (MySQL AUTO_INCREMENT).
            # is_valid_nextcloud_doc_id rejects "0", leading zeros, and Unicode
            # digits that pass str.isdigit(); malformed payloads surface in
            # logs rather than getting swallowed by the broad except below.
            if not is_valid_nextcloud_doc_id(doc_id):
                logger.warning(
                    "Expected numeric news_item doc_id, got %r — skipping document fetch",
                    doc_id,
                )
                return None
            # Fetch news item by ID
            item = await nc_client.news.get_item(int(doc_id))
            # Reconstruct full content as indexed: title + source + URL + body
            # This ensures chunk offsets align with indexed content structure
            body_markdown = html_to_markdown(item.get("body", ""))
            item_title = item.get("title", "")
            item_url = item.get("url", "")
            feed_title = item.get("feedTitle", "")

            content_parts = [item_title]
            if feed_title:
                content_parts.append(f"Source: {feed_title}")
            if item_url:
                content_parts.append(f"URL: {item_url}")
            content_parts.append("")  # Blank line
            content_parts.append(body_markdown)
            return "\n".join(content_parts)
        elif doc_type == "deck_card":
            # Deck card IDs are positive ASCII integers (MySQL AUTO_INCREMENT).
            # is_valid_nextcloud_doc_id rejects "0", leading zeros, and Unicode
            # digits that pass str.isdigit(); malformed payloads surface in
            # logs rather than getting swallowed by the broad except below.
            # The numeric check covers both the metadata-fast-path and the
            # iteration fallback below.
            if not is_valid_nextcloud_doc_id(doc_id):
                logger.warning(
                    "Expected numeric deck_card doc_id, got %r — skipping document fetch",
                    doc_id,
                )
                return None
            # Fetch card from Deck API
            # Try to get board_id/stack_id from Qdrant metadata (O(1) lookup)
            # Otherwise fall back to iteration (legacy data)
            card = None
            deck_metadata = await _get_deck_metadata_from_qdrant(user_id, doc_id)

            if deck_metadata:
                # Fast path: Direct lookup with known board_id/stack_id
                board_id = deck_metadata["board_id"]
                stack_id = deck_metadata["stack_id"]
                try:
                    card = await nc_client.deck.get_card(
                        board_id=board_id, stack_id=stack_id, card_id=int(doc_id)
                    )
                    logger.debug(
                        "Retrieved deck card %s using metadata (board_id=%s, stack_id=%s)",
                        doc_id,
                        board_id,
                        stack_id,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to fetch card with metadata (board_id=%s, stack_id=%s, card_id=%s): %s, falling back to iteration",
                        board_id,
                        stack_id,
                        doc_id,
                        e,
                    )

            # Fallback: Iterate through all boards/stacks (for legacy data or if fast path failed)
            if card is None:
                boards = await nc_client.deck.get_boards()
                card_found = False

                for board in boards:
                    if card_found:
                        break

                    # Skip deleted boards (soft delete: deletedAt > 0)
                    if board.deletedAt > 0:
                        logger.debug(
                            "Skipping deleted board %s while searching for card %s",
                            board.id,
                            doc_id,
                        )
                        continue

                    stacks = await nc_client.deck.get_stacks(board.id)

                    for stack in stacks:
                        if card_found:
                            break
                        if stack.cards:
                            # get_stacks() always yields full DeckCard objects;
                            # the DeckCardSummary projection only happens in the
                            # tool layer, never on freshly-fetched stacks.
                            for c in cast(list[DeckCard], stack.cards):
                                if c.id == int(doc_id):
                                    card = c
                                    card_found = True
                                    logger.debug(
                                        "Found deck card %s in board %s, stack %s (fallback iteration)",
                                        doc_id,
                                        board.id,
                                        stack.id,
                                    )
                                    break

                if not card_found:
                    logger.warning("Deck card %s not found in any board/stack", doc_id)
                    return None

            # Type narrowing: card is set if we reach here
            assert card is not None

            # Reconstruct full content as indexed: title + "\n\n" + description
            # This ensures chunk offsets align with indexed content structure
            content_parts = [card.title]
            if card.description:
                content_parts.append(card.description)
            return "\n\n".join(content_parts)
        elif doc_type == "mail_message":
            # Mail message IDs are positive ASCII integers (MySQL AUTO_INCREMENT).
            if not is_valid_nextcloud_doc_id(doc_id):
                logger.warning(
                    "Expected numeric mail_message doc_id, got %r — skipping document fetch",
                    doc_id,
                )
                return None
            # Reconstruct full content via the shared helper so chunk offsets
            # match what the processor indexed (single source of truth).
            message = await nc_client.mail.get_message(int(doc_id))
            # Empty payload (OCS data=null with a <400 meta) -> skip context
            # expansion, mirroring the processor's index-time guard.
            if not message:
                return None
            return build_mail_content(message)
        else:
            logger.warning("Unsupported doc_type for context expansion: %s", doc_type)
            return None
    except Exception as e:
        logger.error(
            "Error fetching document %s %s: %s", doc_type, doc_id, e, exc_info=True
        )
        return None


def _insert_position_markers(
    before_context: str,
    chunk_text: str,
    after_context: str,
    page_number: int | None,
    chunk_index: int | None,
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
        chunk_index: Zero-based chunk index, or None when the caller didn't
            supply it (rendered as "Chunk ?/N" instead of "Chunk 0/N").
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
    if chunk_index is None:
        position_parts.append(f"Chunk ?/{total_chunks}")
    else:
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
