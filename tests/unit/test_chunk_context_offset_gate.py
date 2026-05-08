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
