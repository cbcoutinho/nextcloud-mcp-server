"""Unit tests for the indexing-path usage-metering helper (Deck #67).

``record_indexing_usage`` records the billable events after a document's chunks
are embedded: ``tokens_embedded`` for every document, and ``pages_embedded``
only for parsed files (real ``page_count``). Text content (no ``page_count``)
meters tokens only — ``pages_embedded`` is a charge for parsing, not content
size (card #282). These cover the value mapping, the flag/zero-chunk no-ops, the
text-only path, and the best-effort failure path without standing up the full
document pipeline.
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
async def test_parsed_file_records_pages_and_tokens(store_spy):
    """A parsed PDF fires both events: pages_embedded = real page count."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        chunk_count=110,
        token_count=4242,
        total_chars=170826,
        page_count=12,
    )

    calls = store_spy.record_usage_event.await_args_list
    by_metric = {c.kwargs["metric"]: c.kwargs["value"] for c in calls}
    # pages_embedded is the real parsed-page count, NOT the chunk count.
    assert by_metric == {"pages_embedded": 12, "tokens_embedded": 4242}
    # Intentional ordering: tokens (recorded for every doc) before pages (the
    # conditional parsing cost). Asserted so a refactor can't silently reverse
    # it — a comment alone is easier to delete than a failing test.
    assert calls[0].kwargs["metric"] == "tokens_embedded"
    assert calls[1].kwargs["metric"] == "pages_embedded"
    for c in calls:
        # Hot-path fast-gate + tenant-local attribution metadata.
        assert c.kwargs["enabled"] is True
        assert c.kwargs["metadata"]["provider"] == "mistral"
        assert c.kwargs["metadata"]["model"] == "mistral-embed"
        assert c.kwargs["metadata"]["user_id"] == "alice"
        assert c.kwargs["metadata"]["doc_type"] == "file"


@pytest.mark.unit
async def test_text_doc_records_tokens_only(store_spy):
    """Unparsed text content (no page_count) meters tokens, never pages."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="note",
        user_id="alice",
        chunk_count=4,
        token_count=512,
        total_chars=7000,
        page_count=None,
    )

    calls = store_spy.record_usage_event.await_args_list
    by_metric = {c.kwargs["metric"]: c.kwargs["value"] for c in calls}
    assert by_metric == {"tokens_embedded": 512}
    assert "pages_embedded" not in by_metric


@pytest.mark.unit
async def test_zero_pages_skips_pages(store_spy):
    """page_count=0 (e.g. an empty/corrupt PDF) records tokens but no pages."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        chunk_count=4,
        token_count=99,
        total_chars=1000,
        page_count=0,
    )

    calls = store_spy.record_usage_event.await_args_list
    by_metric = {c.kwargs["metric"]: c.kwargs["value"] for c in calls}
    assert by_metric == {"tokens_embedded": 99}


@pytest.mark.unit
async def test_negative_pages_skips_pages(store_spy):
    """A malformed negative page_count meters as 'no pages' (tokens only)."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        chunk_count=4,
        token_count=99,
        total_chars=1000,
        page_count=-1,
    )

    calls = store_spy.record_usage_event.await_args_list
    by_metric = {c.kwargs["metric"]: c.kwargs["value"] for c in calls}
    assert by_metric == {"tokens_embedded": 99}


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
        page_count=3,
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
        page_count=3,
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
        page_count=2,
    )
