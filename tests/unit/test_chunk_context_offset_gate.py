"""Unit tests for `nextcloud_mcp_server.search.context.get_chunk_with_context`.

Focused on the chunk-lookup gate that decides whether to fall back from the
indexed `chunk_index` path to the unindexed `(chunk_start, chunk_end)` path.
The behaviour matters because Qdrant Cloud's strict mode rejects filters on
unindexed fields with HTTP 400 — a fall-through there surfaces a misleading
`logger.error` even when the caller's request would correctly resolve as a
404 via the file fast-fail.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import via the auth surface first to side-step a known circular-init issue
# in `nextcloud_mcp_server.search.__init__` when `search` is imported as the
# first entry point (also affects pre-existing tests under tests/unit/search/).
import nextcloud_mcp_server.auth.viz_routes  # noqa: F401  (init-order fixup)
from nextcloud_mcp_server.search import context as context_module
from nextcloud_mcp_server.search.context import get_chunk_with_context

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_nc_client() -> MagicMock:
    return MagicMock()


class TestOffsetFallbackGate:
    """When chunk_index is provided AND doc_type=='file', the offset fallback
    must be skipped — see PR #767 review (🟡 spurious Qdrant error log).
    """

    async def test_file_with_chunk_index_skips_offset_fallback_on_miss(
        self, mock_nc_client
    ):
        with (
            patch.object(
                context_module,
                "_get_chunk_by_index_from_qdrant",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_indexed,
            patch.object(
                context_module,
                "_get_chunk_from_qdrant",
                new_callable=AsyncMock,
                return_value="should-not-be-returned",
            ) as mock_offset,
        ):
            result = await get_chunk_with_context(
                nc_client=mock_nc_client,
                user_id="alice",
                doc_id=12345,
                doc_type="file",
                chunk_start=0,
                chunk_end=100,
                chunk_index=3,
                total_chunks=20,
            )

        assert result is None, "file fast-fail must return None on Qdrant miss"
        mock_indexed.assert_awaited_once()
        mock_offset.assert_not_awaited()

    async def test_note_with_chunk_index_still_uses_offset_fallback(
        self, mock_nc_client
    ):
        """Notes/deck cards keep the offset fallback (cheap, useful for legacy
        data): the gate is file-specific.
        """
        with (
            patch.object(
                context_module,
                "_get_chunk_by_index_from_qdrant",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_indexed,
            patch.object(
                context_module,
                "_get_chunk_from_qdrant",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_offset,
            patch.object(
                context_module,
                "_fetch_document_text",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await get_chunk_with_context(
                nc_client=mock_nc_client,
                user_id="alice",
                doc_id=42,
                doc_type="note",
                chunk_start=0,
                chunk_end=10,
                chunk_index=2,
                total_chunks=5,
            )

        mock_indexed.assert_awaited_once()
        mock_offset.assert_awaited_once()

    async def test_file_without_chunk_index_uses_offset_fallback(self, mock_nc_client):
        """Files with no chunk_index supplied still use the offset path —
        the gate only kicks in once the indexed lookup has been attempted.
        """
        with (
            patch.object(
                context_module,
                "_get_chunk_by_index_from_qdrant",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_indexed,
            patch.object(
                context_module,
                "_get_chunk_from_qdrant",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_offset,
        ):
            await get_chunk_with_context(
                nc_client=mock_nc_client,
                user_id="alice",
                doc_id=12345,
                doc_type="file",
                chunk_start=0,
                chunk_end=100,
                chunk_index=None,
                total_chunks=20,
            )

        mock_indexed.assert_not_awaited()
        mock_offset.assert_awaited_once()


class TestNullableChunkIndexPropagation:
    """When the caller doesn't supply chunk_index, it must propagate as None
    through to ChunkContext and the position markers — distinguishing
    "unknown position" from "actually chunk 0". See PR #767 review (🟡 issue 2).
    """

    async def test_fast_path_without_chunk_index_returns_none_in_response(
        self, mock_nc_client
    ):
        """Note retrieved via offset fallback (chunk_index=None) → response
        chunk_index is None, markers render '?/N', and adjacent fetch is
        skipped (would otherwise produce wrong neighbours from index 0).
        """
        with (
            patch.object(
                context_module,
                "_get_chunk_by_index_from_qdrant",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_indexed,
            patch.object(
                context_module,
                "_get_chunk_from_qdrant",
                new_callable=AsyncMock,
                return_value="matched chunk text",
            ),
        ):
            result = await get_chunk_with_context(
                nc_client=mock_nc_client,
                user_id="alice",
                doc_id=42,
                doc_type="note",
                chunk_start=100,
                chunk_end=200,
                chunk_index=None,
                total_chunks=8,
            )

        assert result is not None
        assert result.chunk_index is None, (
            "chunk_index must propagate as None, not default to 0"
        )
        assert "Chunk ?/8" in result.marked_text
        assert "Chunk 1 of 8" not in result.marked_text
        # Adjacent fetch must be skipped — index arithmetic from 0 would
        # query the wrong neighbours when actual position isn't 0.
        mock_indexed.assert_not_awaited()
        assert result.has_before_truncation is True
        assert result.has_after_truncation is True

    async def test_fast_path_with_chunk_index_renders_position_correctly(
        self, mock_nc_client
    ):
        """Counter-positive: when chunk_index is supplied, response carries
        the value and markers render the explicit "Chunk N of M".
        """
        with (
            patch.object(
                context_module,
                "_get_chunk_by_index_from_qdrant",
                new_callable=AsyncMock,
                side_effect=[
                    "current chunk text",  # primary lookup
                    "previous chunk text",  # adjacent before
                    "next chunk text",  # adjacent after
                ],
            ),
            patch.object(
                context_module,
                "_get_chunk_from_qdrant",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await get_chunk_with_context(
                nc_client=mock_nc_client,
                user_id="alice",
                doc_id=42,
                doc_type="note",
                chunk_start=0,
                chunk_end=10,
                chunk_index=5,
                total_chunks=20,
            )

        assert result is not None
        assert result.chunk_index == 5
        assert "Chunk 6 of 20" in result.marked_text

    async def test_doc_text_fallback_without_chunk_index_returns_none(
        self, mock_nc_client
    ):
        """Doc-text fallback (Qdrant miss → re-fetch document) must also
        propagate chunk_index=None into the response so callers can tell
        the position is unknown.
        """
        with (
            patch.object(
                context_module,
                "_get_chunk_by_index_from_qdrant",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                context_module,
                "_get_chunk_from_qdrant",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                context_module,
                "_fetch_document_text",
                new_callable=AsyncMock,
                return_value="x" * 500,
            ),
        ):
            result = await get_chunk_with_context(
                nc_client=mock_nc_client,
                user_id="alice",
                doc_id=42,
                doc_type="note",
                chunk_start=100,
                chunk_end=200,
                chunk_index=None,
                total_chunks=10,
            )

        assert result is not None
        assert result.chunk_index is None
        assert "Chunk ?/10" in result.marked_text


class TestPositionMarkers:
    """Direct tests for `_insert_position_markers` rendering when chunk_index
    is None vs explicit.
    """

    def test_marker_renders_question_mark_when_chunk_index_is_none(self):
        text = context_module._insert_position_markers(
            before_context="",
            chunk_text="x",
            after_context="",
            page_number=None,
            chunk_index=None,
            total_chunks=12,
            has_before_truncation=False,
            has_after_truncation=False,
        )
        assert "Chunk ?/12" in text

    def test_marker_renders_explicit_index_when_supplied(self):
        text = context_module._insert_position_markers(
            before_context="",
            chunk_text="x",
            after_context="",
            page_number=3,
            chunk_index=4,
            total_chunks=12,
            has_before_truncation=False,
            has_after_truncation=False,
        )
        assert "Page 3" in text
        assert "Chunk 5 of 12" in text
