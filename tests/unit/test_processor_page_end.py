"""Unit tests for the ``page_end`` citation-range payload fallback (Deck #636).

``_index_document`` stamps ``page_end`` on every file chunk's Qdrant payload via
:func:`processor.resolve_page_end`. Packed multi-page chunks carry a real range
(first page = ``page_number``, last = ``page_end``); every other chunk leaves
``page_end`` unset and must fall back to ``page_number`` so the citation range is
always present. The char-based path is the case that matters:
:func:`processor.assign_page_numbers` back-fills ``page_number`` post-hoc but
never touches ``page_end``, so without the fallback those chunks would ship
``page_end=None`` while ``page_number`` is set.
"""

import pytest

from nextcloud_mcp_server.vector.document_chunker import ChunkWithPosition
from nextcloud_mcp_server.vector.processor import (
    assign_page_numbers,
    resolve_page_end,
)


@pytest.mark.unit
def test_resolve_page_end_preserves_packed_range():
    """A packed chunk's explicit page range is passed through unchanged."""
    chunk = ChunkWithPosition(
        text="one two three",
        start_offset=0,
        end_offset=13,
        page_number=1,
        page_end=3,
    )
    assert resolve_page_end(chunk) == 3


@pytest.mark.unit
def test_resolve_page_end_single_page_chunk():
    """A single-page chunk reports ``page_end == page_number``."""
    chunk = ChunkWithPosition(
        text="solo",
        start_offset=0,
        end_offset=4,
        page_number=2,
        page_end=2,
    )
    assert resolve_page_end(chunk) == 2


@pytest.mark.unit
def test_resolve_page_end_falls_back_for_char_path_chunk():
    """The char-based path sets page_number but not page_end; fall back to it.

    Drives the real ``assign_page_numbers`` seam: a chunk built by the char
    splitter has ``page_end=None``; after page assignment ``page_number`` is set
    but ``page_end`` is still ``None``, so ``resolve_page_end`` must recover the
    page as the citation end (never leaking ``None`` into the payload).
    """
    # Char-path chunk: no page info yet, page_end left unset by the splitter.
    chunk = ChunkWithPosition(
        text="body text on the second page",
        start_offset=120,
        end_offset=148,
    )
    boundaries = [
        {"page": 1, "start_offset": 0, "end_offset": 100},
        {"page": 2, "start_offset": 100, "end_offset": 200},
    ]

    assign_page_numbers([chunk], boundaries)

    assert chunk.page_number == 2  # back-filled by majority overlap
    assert chunk.page_end is None  # assign_page_numbers never sets page_end
    assert resolve_page_end(chunk) == 2  # fallback recovers the citation page


@pytest.mark.unit
def test_resolve_page_end_none_when_unpaginated():
    """Non-paginated content (no page info at all) stays ``None``."""
    chunk = ChunkWithPosition(text="note body", start_offset=0, end_offset=9)
    assert resolve_page_end(chunk) is None
