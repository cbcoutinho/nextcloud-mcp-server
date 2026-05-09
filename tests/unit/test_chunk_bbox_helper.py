"""Unit tests for
`nextcloud_mcp_server.search.context.get_chunk_bbox_and_page_from_qdrant`.

Covers the two paths the helper handles:
- Indexed lookup via `chunk_index` (the preferred path post
  cbcoutinho/astrolabe#75)
- Legacy offset fallback via `(chunk_start_offset, chunk_end_offset)`, which
  may 400 in Qdrant Cloud strict mode

Plus the regression case from PR #767 review: when the payload has
`chunk_bbox` but no `page_number`, the helper must surface that as
`(bbox, None)` so callers can preserve their context-derived page_number
fallback.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import via the auth surface first to side-step the known
# `nextcloud_mcp_server.search.__init__` circular-init issue (same workaround
# used in test_chunk_context_offset_gate.py).
import nextcloud_mcp_server.auth.viz_routes  # noqa: F401
from nextcloud_mcp_server.search import context as context_module
from nextcloud_mcp_server.search.context import get_chunk_bbox_and_page_from_qdrant

pytestmark = pytest.mark.unit


def _make_point(payload: dict) -> MagicMock:
    point = MagicMock()
    point.payload = payload
    return point


def _patch_qdrant(scroll_return=None, scroll_side_effect=None):
    qdrant_client = MagicMock()
    if scroll_side_effect is not None:
        qdrant_client.scroll = AsyncMock(side_effect=scroll_side_effect)
    else:
        qdrant_client.scroll = AsyncMock(return_value=scroll_return)
    return patch.object(
        context_module,
        "get_qdrant_client",
        new_callable=AsyncMock,
        return_value=qdrant_client,
    ), qdrant_client


class TestIndexedPath:
    """When `chunk_index` is supplied, the helper must use the indexed
    `chunk_index` filter (not the offset fallback)."""

    async def test_returns_bbox_and_page_when_payload_complete(self):
        bbox = [[0, 0, 100, 50]]
        point = _make_point({"chunk_bbox": bbox, "page_number": 7})
        ctx, qdrant_client = _patch_qdrant(scroll_return=([point], None))
        with ctx:
            result = await get_chunk_bbox_and_page_from_qdrant(
                user_id="alice",
                doc_id="42",
                chunk_index=3,
                chunk_start=0,
                chunk_end=100,
            )

        assert result == (bbox, 7)
        # One scroll call, and the filter must include chunk_index (not offsets)
        qdrant_client.scroll.assert_awaited_once()
        scroll_kwargs = qdrant_client.scroll.await_args.kwargs
        filter_keys = [c.key for c in scroll_kwargs["scroll_filter"].must]
        assert "chunk_index" in filter_keys
        assert "chunk_start_offset" not in filter_keys
        assert "chunk_end_offset" not in filter_keys


class TestOffsetFallbackPath:
    """When `chunk_index` is None, the helper must use the offset filter."""

    async def test_returns_bbox_and_page_when_payload_complete(self):
        bbox = [[10, 20, 110, 70]]
        point = _make_point({"chunk_bbox": bbox, "page_number": 2})
        ctx, qdrant_client = _patch_qdrant(scroll_return=([point], None))
        with ctx:
            result = await get_chunk_bbox_and_page_from_qdrant(
                user_id="bob",
                doc_id="99",
                chunk_index=None,
                chunk_start=500,
                chunk_end=600,
            )

        assert result == (bbox, 2)
        scroll_kwargs = qdrant_client.scroll.await_args.kwargs
        filter_keys = [c.key for c in scroll_kwargs["scroll_filter"].must]
        assert "chunk_start_offset" in filter_keys
        assert "chunk_end_offset" in filter_keys
        assert "chunk_index" not in filter_keys

    async def test_strict_mode_400_returns_none_pair_and_warns(self, caplog):
        """Qdrant Cloud strict mode 400s on unindexed offset filters; the
        helper must swallow the exception, log a warning, and degrade
        gracefully so the route can still return chunk text."""
        ctx, _ = _patch_qdrant(scroll_side_effect=Exception("strict mode: 400"))
        with ctx, caplog.at_level("WARNING"):
            result = await get_chunk_bbox_and_page_from_qdrant(
                user_id="bob",
                doc_id="99",
                chunk_index=None,
                chunk_start=0,
                chunk_end=100,
            )

        assert result == (None, None)
        assert any("Failed to fetch chunk bbox" in r.message for r in caplog.records)


class TestPayloadShape:
    """Each payload field can be missing independently — callers rely on
    that to decide whether to overwrite their fallback values."""

    async def test_empty_points_returns_none_pair(self):
        ctx, _ = _patch_qdrant(scroll_return=([], None))
        with ctx:
            result = await get_chunk_bbox_and_page_from_qdrant(
                user_id="alice",
                doc_id="1",
                chunk_index=0,
                chunk_start=0,
                chunk_end=10,
            )

        assert result == (None, None)

    async def test_missing_page_returns_bbox_only(self):
        """Regression for PR #767 review issue #1: when Qdrant returns a
        point whose payload lacks `page_number`, the helper must return
        `(bbox, None)` so callers preserve their `chunk_context.page_number`
        fallback rather than clobbering it to None."""
        bbox = [[0, 0, 100, 50]]
        point = _make_point({"chunk_bbox": bbox})  # no page_number
        ctx, _ = _patch_qdrant(scroll_return=([point], None))
        with ctx:
            result = await get_chunk_bbox_and_page_from_qdrant(
                user_id="alice",
                doc_id="42",
                chunk_index=3,
                chunk_start=0,
                chunk_end=100,
            )

        assert result == (bbox, None)

    async def test_missing_bbox_returns_page_only(self):
        point = _make_point({"page_number": 5})  # no chunk_bbox
        ctx, _ = _patch_qdrant(scroll_return=([point], None))
        with ctx:
            result = await get_chunk_bbox_and_page_from_qdrant(
                user_id="alice",
                doc_id="42",
                chunk_index=3,
                chunk_start=0,
                chunk_end=100,
            )

        assert result == (None, 5)

    async def test_empty_payload_returns_none_pair(self):
        point = _make_point({})
        ctx, _ = _patch_qdrant(scroll_return=([point], None))
        with ctx:
            result = await get_chunk_bbox_and_page_from_qdrant(
                user_id="alice",
                doc_id="42",
                chunk_index=3,
                chunk_start=0,
                chunk_end=100,
            )

        assert result == (None, None)

    async def test_falsy_payload_treated_as_no_point(self):
        """`if not points[0].payload` short-circuits when payload is None or
        an empty dict, mirroring the original guards in the route handlers."""
        point = MagicMock()
        point.payload = None
        ctx, _ = _patch_qdrant(scroll_return=([point], None))
        with ctx:
            result = await get_chunk_bbox_and_page_from_qdrant(
                user_id="alice",
                doc_id="42",
                chunk_index=3,
                chunk_start=0,
                chunk_end=100,
            )

        assert result == (None, None)


class TestExceptionHandling:
    """Any error from Qdrant must produce `(None, None)` — never propagate."""

    async def test_indexed_path_exception_returns_none_pair(self, caplog):
        ctx, _ = _patch_qdrant(scroll_side_effect=RuntimeError("qdrant unavailable"))
        with ctx, caplog.at_level("WARNING"):
            result = await get_chunk_bbox_and_page_from_qdrant(
                user_id="alice",
                doc_id="42",
                chunk_index=3,
                chunk_start=0,
                chunk_end=100,
            )

        assert result == (None, None)
        assert any("Failed to fetch chunk bbox" in r.message for r in caplog.records)
