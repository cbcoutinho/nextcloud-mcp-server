"""Unit tests for the scanner's Qdrant scroll helpers.

The vector-sync scanner runs in-process inside the always-on per-tenant API
Pod, so its deletion-tracking scrolls must stay bounded by *page size* rather
than by the tenant's indexed-point count. An earlier version paginated the
fetch but accumulated every page into one returned list, which scaled peak RSS
with corpus size and OOMKilled the API Pod on a large tenant.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from qdrant_client.models import Filter

from nextcloud_mcp_server.vector.scanner import _iter_all_points, _scroll_doc_ids

pytestmark = pytest.mark.unit


def _point(doc_id: str | None) -> SimpleNamespace:
    """A Record stand-in: only ``.payload`` is read by the helpers."""
    return SimpleNamespace(payload=None if doc_id is None else {"doc_id": doc_id})


def _paged_client(pages: list[list[Any]]) -> SimpleNamespace:
    """Fake AsyncQdrantClient whose scroll() hands back one page per call.

    ``calls`` records every scroll() invocation so a test can assert how many
    round-trips happened at a given point in the consumption.
    """
    calls: list[dict[str, Any]] = []

    async def scroll(**kwargs: Any):
        calls.append(kwargs)
        index = kwargs.get("offset") or 0
        next_offset = index + 1 if index + 1 < len(pages) else None
        return pages[index], next_offset

    return SimpleNamespace(scroll=scroll, calls=calls)


async def test_scroll_doc_ids_collects_every_page():
    client = _paged_client([[_point("a"), _point("b")], [_point("c")], [_point("d")]])

    doc_ids = await _scroll_doc_ids(
        client,  # ty: ignore[invalid-argument-type]
        collection_name="col",
        scroll_filter=Filter(must=[]),
    )

    assert doc_ids == {"a", "b", "c", "d"}
    assert len(client.calls) == 3


async def test_scroll_doc_ids_skips_points_without_a_doc_id():
    client = _paged_client([[_point("a"), _point(None), SimpleNamespace(payload={})]])

    doc_ids = await _scroll_doc_ids(
        client,  # ty: ignore[invalid-argument-type]
        collection_name="col",
        scroll_filter=Filter(must=[]),
    )

    assert doc_ids == {"a"}


async def test_iter_all_points_yields_before_fetching_later_pages():
    """Regression guard: the helper must stream, never accumulate.

    Consuming the first point may only have cost one round-trip. If someone
    re-introduces "fetch every page into a list, then return it", every page is
    fetched before the first point is yielded and this fails.
    """
    client = _paged_client([[_point("a")], [_point("b")], [_point("c")]])

    points = _iter_all_points(
        client,  # ty: ignore[invalid-argument-type]
        collection_name="col",
        scroll_filter=Filter(must=[]),
        payload_fields=["doc_id"],
    )
    try:
        first = await anext(points)
        assert first.payload == {"doc_id": "a"}
        assert len(client.calls) == 1
    finally:
        await points.aclose()


async def test_iter_all_points_requests_the_configured_page_size():
    client = _paged_client([[_point("a")]])

    collected = [
        point.payload["doc_id"]
        async for point in _iter_all_points(
            client,  # ty: ignore[invalid-argument-type]
            collection_name="col",
            scroll_filter=Filter(must=[]),
            payload_fields=["doc_id"],
            page_size=7,
        )
    ]

    assert collected == ["a"]
    assert client.calls[0]["limit"] == 7
    assert client.calls[0]["with_vectors"] is False
