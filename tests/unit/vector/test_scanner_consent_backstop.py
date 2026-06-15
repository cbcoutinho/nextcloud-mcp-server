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

from nextcloud_mcp_server.vector import scanner as scanner_module
from nextcloud_mcp_server.vector.queue.ports import TaskProducer
from nextcloud_mcp_server.vector.scanner import _enqueue_deletes_for_disabled_types


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
    allowed = frozenset({"note", "news_item", "deck_card", "file"})
    queued = await _enqueue_deletes_for_disabled_types(
        "alice", _producer(send), allowed, 1
    )
    assert queued == 0
    send.assert_not_called()
