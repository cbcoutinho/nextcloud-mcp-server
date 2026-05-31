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


async def test_run_signals_started_then_retries_subscribe(mocker, monkeypatch):
    """run() signals started before subscribing, retries a failed subscribe,
    and consumes messages once subscribed."""
    import anyio

    # Make backoff sleeps instant so the retry path doesn't stall the test.
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(anyio, "sleep", _no_sleep)

    store = StatusStore()
    js = mocker.AsyncMock()

    # First subscribe attempt fails (broker not ready), second succeeds.
    fake_sub = mocker.AsyncMock()
    js.pull_subscribe.side_effect = [ConnectionError("broker not ready"), fake_sub]

    shutdown = anyio.Event()
    msg = mocker.Mock()
    msg.subject = "mcp.document.ready.t1"
    msg.data = json.dumps({"doc_id": "d1", "content_hash": "h1"}).encode()
    msg.ack = mocker.AsyncMock()

    fetches = {"n": 0}

    async def _fetch(*_a, **_k):
        fetches["n"] += 1
        if fetches["n"] == 1:
            return [msg]
        shutdown.set()  # stop the loop after the first batch is handled
        return []

    fake_sub.fetch.side_effect = _fetch

    task_status = mocker.Mock()
    subscriber = NatsStatusSubscriber(
        nc=mocker.AsyncMock(), js=js, tenant_id="t1", store=store
    )

    await subscriber.run(shutdown, task_status=task_status)

    # started() fires before any subscribe attempt and exactly once.
    task_status.started.assert_called_once()
    # The failed first subscribe was retried (two attempts total).
    assert js.pull_subscribe.call_count == 2
    # The message from the successful subscription was recorded + acked.
    assert store.get("d1") == {
        "state": "ready",
        "content_hash": "h1",
        "transitioned_at": None,
    }
    msg.ack.assert_awaited_once()


async def test_run_resubscribes_after_fetch_error(mocker, monkeypatch):
    """A non-timeout fetch error drops the subscription and re-subscribes."""
    import anyio
    import nats.errors

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(anyio, "sleep", _no_sleep)

    store = StatusStore()
    js = mocker.AsyncMock()
    first_sub = mocker.AsyncMock()
    second_sub = mocker.AsyncMock()
    js.pull_subscribe.side_effect = [first_sub, second_sub]

    shutdown = anyio.Event()

    # first_sub.fetch raises a real broker error → re-subscribe.
    first_sub.fetch.side_effect = ConnectionResetError("broker dropped")

    # second_sub.fetch idles once (timeout) then stops the loop.
    fetches = {"n": 0}

    async def _second_fetch(*_a, **_k):
        fetches["n"] += 1
        if fetches["n"] == 1:
            raise nats.errors.TimeoutError
        shutdown.set()
        return []

    second_sub.fetch.side_effect = _second_fetch

    subscriber = NatsStatusSubscriber(
        nc=mocker.AsyncMock(), js=js, tenant_id="t1", store=store
    )
    await subscriber.run(shutdown)

    # Re-subscribed after the fetch error (two subscriptions used).
    assert js.pull_subscribe.call_count == 2
