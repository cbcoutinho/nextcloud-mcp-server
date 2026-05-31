"""StatusStore + NATS status message handling (design §10.1, STATUS_BACKEND=bus)."""

import json

from nextcloud_mcp_server.vector.queue.status import (
    NatsStatusSubscriber,
    StatusStore,
    state_from_subject,
)


def test_store_records_and_counts():
    store = StatusStore()
    store.record("d1", "ready", content_hash="h1")
    store.record("d2", "failed")
    store.record("d1", "ready", content_hash="h1")  # idempotent overwrite
    assert len(store) == 2
    assert store.counts() == {"ready": 1, "failed": 1}
    assert store.get("d1")["content_hash"] == "h1"


def test_store_is_bounded_lru():
    store = StatusStore(max_size=2)
    store.record("d1", "ready")
    store.record("d2", "ready")
    store.record("d3", "ready")  # evicts d1
    assert len(store) == 2
    assert store.get("d1") is None
    assert store.get("d3") is not None


def test_state_from_subject():
    assert state_from_subject("mcp.document.ready.tenant-1") == "ready"
    assert state_from_subject("mcp.document.failed.tenant-1") == "failed"
    assert state_from_subject("mcp.document.reparsed.tenant-1") == "reparsed"
    assert state_from_subject("mcp.document.bogus.tenant-1") is None
    assert state_from_subject("mcp.ingest.requested.tenant-1") is None


def test_handle_message_records_state():
    store = StatusStore()
    events = []
    sub = NatsStatusSubscriber(
        nc=None,
        js=None,
        tenant_id="t1",
        store=store,
        on_event=lambda d, s: events.append((d, s)),
    )
    payload = json.dumps(
        {
            "tenant_id": "t1",
            "doc_id": "doc-9",
            "content_hash": "abc",
            "transitioned_at": "2026-05-27T00:00:00Z",
        }
    ).encode()
    sub.handle_message("mcp.document.ready.t1", payload)
    entry = store.get("doc-9")
    assert entry["state"] == "ready"
    assert entry["content_hash"] == "abc"
    assert events == [("doc-9", "ready")]


def test_handle_message_ignores_bad_payload_and_subject():
    store = StatusStore()
    sub = NatsStatusSubscriber(nc=None, js=None, tenant_id="t1", store=store)
    sub.handle_message("mcp.document.ready.t1", b"not json")
    sub.handle_message("mcp.ingest.requested.t1", b'{"doc_id":"x"}')
    assert len(store) == 0
