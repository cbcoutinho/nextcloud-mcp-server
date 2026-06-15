"""Unit tests for global purge-by-doc-type (admin consent enforcement)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import nextcloud_mcp_server.vector.purge as purge_module
from nextcloud_mcp_server.vector.purge import purge_doc_types

pytestmark = pytest.mark.unit


def _patch_qdrant(monkeypatch, *, counts: dict[str, int], delete_raises=None):
    """Wire a fake Qdrant client whose ``count`` reflects ``counts`` per
    doc_type (read off the filter's MatchValue) and whose ``delete`` optionally
    raises for given doc_types."""
    client = AsyncMock()

    def _doc_type_of(flt):
        return flt.must[0].match.value

    # Sync side_effects: AsyncMock awaits the call and returns the value, so the
    # helpers don't need to be coroutines themselves.
    def fake_count(*, collection_name, count_filter, exact):
        return SimpleNamespace(count=counts.get(_doc_type_of(count_filter), 0))

    def fake_delete(*, collection_name, points_selector):
        dt = _doc_type_of(points_selector)
        if delete_raises and dt in delete_raises:
            raise RuntimeError(f"delete failed for {dt}")

    client.count.side_effect = fake_count
    client.delete.side_effect = fake_delete

    monkeypatch.setattr(
        purge_module, "get_qdrant_client", AsyncMock(return_value=client)
    )
    monkeypatch.setattr(
        purge_module,
        "get_settings",
        lambda: SimpleNamespace(get_collection_name=lambda: "test_collection"),
    )
    return client


async def test_purges_each_doc_type_and_reports_counts(monkeypatch):
    client = _patch_qdrant(monkeypatch, counts={"file": 7, "note": 3})

    result = await purge_doc_types(["file", "note"])

    assert result == {"file": 7, "note": 3}
    assert client.delete.await_count == 2


async def test_purge_is_owner_agnostic_global(monkeypatch):
    # The admin disable is global, so the delete filter must match by doc_type
    # ONLY — no owner_id/user_id condition that would scope it to one user.
    client = _patch_qdrant(monkeypatch, counts={"file": 1})

    await purge_doc_types(["file"])

    flt = client.delete.await_args.kwargs["points_selector"]
    keys = [c.key for c in flt.must]
    assert keys == ["doc_type"]
    assert flt.must[0].match.value == "file"


async def test_dedupes_doc_types(monkeypatch):
    client = _patch_qdrant(monkeypatch, counts={"file": 2})

    result = await purge_doc_types(["file", "file"])

    assert result == {"file": 2}
    assert client.delete.await_count == 1


async def test_zero_points_is_safe(monkeypatch):
    _patch_qdrant(monkeypatch, counts={})
    assert await purge_doc_types(["deck_card"]) == {"deck_card": 0}


async def test_partial_failure_returns_partial(monkeypatch):
    _patch_qdrant(
        monkeypatch,
        counts={"file": 5, "note": 4},
        delete_raises={"note"},
    )
    # "note" delete fails, "file" succeeds — partial progress is returned.
    result = await purge_doc_types(["file", "note"])
    assert result == {"file": 5}


async def test_total_failure_raises(monkeypatch):
    _patch_qdrant(monkeypatch, counts={"file": 5}, delete_raises={"file"})
    with pytest.raises(RuntimeError):
        await purge_doc_types(["file"])
