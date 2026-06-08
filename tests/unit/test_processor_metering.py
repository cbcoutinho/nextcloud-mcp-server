"""Unit tests for the indexing-path usage-metering helper (Deck #67).

``record_indexing_usage`` records the two billable events (``pages_embedded`` +
``tokens_embedded``) after a document's chunks are embedded. These cover the
value mapping, the flag/zero-chunk no-ops, and the best-effort failure path
without standing up the full document pipeline.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.vector import processor


@pytest.fixture
def store_spy(monkeypatch):
    """Patch UsageEventStore.shared() to return a spy store."""
    store = MagicMock()
    store.record_usage_event = AsyncMock()
    monkeypatch.setattr(
        processor.UsageEventStore, "shared", AsyncMock(return_value=store)
    )
    return store


@pytest.mark.unit
async def test_records_pages_embedded_and_token_count(store_spy):
    """Both events fire: pages_embedded = chunk count, tokens_embedded = tokens."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        chunk_count=110,
        token_count=4242,
        total_chars=170826,
    )

    calls = store_spy.record_usage_event.await_args_list
    by_metric = {c.kwargs["metric"]: c.kwargs["value"] for c in calls}
    assert by_metric == {"pages_embedded": 110, "tokens_embedded": 4242}
    for c in calls:
        # Hot-path fast-gate + tenant-local attribution metadata.
        assert c.kwargs["enabled"] is True
        assert c.kwargs["metadata"]["provider"] == "mistral"
        assert c.kwargs["metadata"]["model"] == "mistral-embed"
        assert c.kwargs["metadata"]["user_id"] == "alice"
        assert c.kwargs["metadata"]["doc_type"] == "file"


@pytest.mark.unit
async def test_disabled_is_noop(store_spy):
    """Flag off → no store access, no events."""
    await processor.record_indexing_usage(
        enabled=False,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        chunk_count=10,
        token_count=20,
        total_chars=5,
    )
    store_spy.record_usage_event.assert_not_awaited()


@pytest.mark.unit
async def test_zero_chunks_is_noop(store_spy):
    """A document with no chunks records nothing (no zero-value rows)."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        chunk_count=0,
        token_count=0,
        total_chars=0,
    )
    store_spy.record_usage_event.assert_not_awaited()


@pytest.mark.unit
async def test_store_failure_is_swallowed(monkeypatch):
    """A store-construction failure is logged, never raised into indexing."""
    monkeypatch.setattr(
        processor.UsageEventStore,
        "shared",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    # Must not raise.
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        chunk_count=3,
        token_count=7,
        total_chars=9,
    )
