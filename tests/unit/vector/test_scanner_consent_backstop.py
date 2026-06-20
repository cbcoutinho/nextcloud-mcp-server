"""Unit tests for the scanner's admin-consent backstop deletion.

When an admin disables a text source (note/news_item/deck_card), the scanner
skips its scan_* function, so the in-function deletion-tracking never runs. The
backstop enqueues deletes for any indexed points of the disabled type, mirroring
the files path — but only on a concrete allow-set (never on fail-open None).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from nextcloud_mcp_server.vector import scanner as scanner_module
from nextcloud_mcp_server.vector.queue.ports import TaskProducer
from nextcloud_mcp_server.vector.scanner import _enqueue_deletes_for_disabled_types

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_backstop_state():
    """The one-shot guard is module-level; reset it between tests."""
    scanner_module._consent_backstop_done.clear()
    yield
    scanner_module._consent_backstop_done.clear()


def _producer(send: AsyncMock) -> TaskProducer:
    """A minimal stand-in for the TaskProducer protocol (only ``send`` is used)."""
    return cast(TaskProducer, SimpleNamespace(send=send))


def _patch_qdrant(monkeypatch, points_by_type: dict[str, list[str]]):
    client = AsyncMock()

    def fake_scroll(
        *, collection_name, scroll_filter, with_payload, with_vectors, limit, offset
    ):
        # must=[user_id, doc_type] — doc_type is the second condition.
        doc_type = scroll_filter.must[1].match.value
        points = [
            SimpleNamespace(payload={"doc_id": doc_id})
            for doc_id in points_by_type.get(doc_type, [])
        ]
        return (points, None)

    client.scroll.side_effect = fake_scroll
    monkeypatch.setattr(
        scanner_module, "get_qdrant_client", AsyncMock(return_value=client)
    )
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(get_collection_name=lambda: "c"),
    )


async def test_enqueues_deletes_for_disabled_text_type(monkeypatch):
    _patch_qdrant(monkeypatch, {"note": ["n1", "n2"], "deck_card": ["d1"]})
    sent: list = []
    stream = _producer(AsyncMock(side_effect=lambda t: sent.append(t)))

    # note disabled; news_item + deck_card still allowed.
    allowed = frozenset({"file", "news_item", "deck_card"})
    queued = await _enqueue_deletes_for_disabled_types("alice", stream, allowed, 1)

    assert queued == 2
    assert {t.doc_id for t in sent} == {"n1", "n2"}
    assert all(t.operation == "delete" and t.doc_type == "note" for t in sent)


async def test_all_text_types_disabled_enqueues_all(monkeypatch):
    # Admin disabled everything at once (empty allow-set): every text type's
    # indexed points are enqueued for deletion in a single call.
    _patch_qdrant(
        monkeypatch, {"note": ["n1"], "news_item": ["ni1"], "deck_card": ["d1"]}
    )
    sent: list = []
    stream = _producer(AsyncMock(side_effect=lambda t: sent.append(t)))

    queued = await _enqueue_deletes_for_disabled_types("alice", stream, frozenset(), 1)

    assert queued == 3
    assert {(t.doc_type, t.doc_id) for t in sent} == {
        ("note", "n1"),
        ("news_item", "ni1"),
        ("deck_card", "d1"),
    }


async def test_noop_when_allowed_is_none(monkeypatch):
    # Fail-open: a transient capability read must never trigger deletion.
    send = AsyncMock()
    queued = await _enqueue_deletes_for_disabled_types(
        "alice", _producer(send), None, 1
    )
    assert queued == 0
    send.assert_not_called()


async def test_noop_when_all_text_types_allowed(monkeypatch):
    send = AsyncMock()
    # Derive from INDEXED_DOC_TYPES so a newly-indexed text type doesn't make
    # this "all allowed" set silently incomplete (and trip the backstop).
    allowed = frozenset(scanner_module.INDEXED_DOC_TYPES)
    queued = await _enqueue_deletes_for_disabled_types(
        "alice", _producer(send), allowed, 1
    )
    assert queued == 0
    send.assert_not_called()


async def test_one_shot_does_not_reflood_on_subsequent_scans(monkeypatch):
    _patch_qdrant(monkeypatch, {"note": ["n1", "n2"]})
    send = AsyncMock()
    allowed = frozenset({"file"})  # note disabled

    first = await _enqueue_deletes_for_disabled_types(
        "alice", _producer(send), allowed, 1
    )
    second = await _enqueue_deletes_for_disabled_types(
        "alice", _producer(send), allowed, 2
    )

    assert first == 2
    # Standing disable: the next scan must not re-enqueue the same deletes.
    assert second == 0


async def test_re_enable_then_disable_retriggers_backstop(monkeypatch):
    _patch_qdrant(monkeypatch, {"note": ["n1"]})
    send = AsyncMock()
    disabled = frozenset({"file"})
    enabled = frozenset({"file", "note"})

    assert (
        await _enqueue_deletes_for_disabled_types("alice", _producer(send), disabled, 1)
        == 1
    )
    # Re-enabled: clears the one-shot marker.
    assert (
        await _enqueue_deletes_for_disabled_types("alice", _producer(send), enabled, 2)
        == 0
    )
    # Disabled again: backstop fires once more.
    assert (
        await _enqueue_deletes_for_disabled_types("alice", _producer(send), disabled, 3)
        == 1
    )
