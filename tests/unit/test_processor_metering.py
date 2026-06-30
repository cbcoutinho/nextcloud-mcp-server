"""Unit tests for the indexing-path usage-metering helper (Deck #67 / #401).

``record_indexing_usage`` records the billable events after a document's chunks
are embedded: ``tokens_embedded`` for every document, and ``pages_embedded``
only for parsed files (real ``page_count``). Text content (no ``page_count``)
meters tokens only — ``pages_embedded`` is a charge for parsing, not content
size (card #282). The byte-volume dimensions ``bytes_ingested`` /
``bytes_stored`` (card #401) are recorded for every document, skipped only on a
non-positive count. These cover the value mapping, the flag/zero-chunk no-ops,
the text-only path, and the best-effort failure path without standing up the
full document pipeline. (The call site's raw-binary-vs-UTF-8 source selection
for ``bytes_ingested`` lives in the document pipeline and is exercised by the
integration smoke path, not these helper-level tests.)
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
def test_ingested_byte_size_files_use_raw_binary():
    """A file's bytes_ingested is its raw binary size, not the text size."""
    # Raw bytes longer than the decoded text would suggest (e.g. PDF overhead):
    # the helper must report the binary length, ignoring `content`.
    raw = b"%PDF-1.7 ...binary..." + b"\x00" * 500
    assert processor.ingested_byte_size(raw, "short extracted text") == len(raw)


@pytest.mark.unit
def test_ingested_byte_size_text_uses_utf8_length():
    """Text doc types (content_bytes=None) fall back to UTF-8 byte length."""
    # Multibyte char: 1 code point, 2 UTF-8 bytes — len(str) would undercount.
    text = "café"
    assert processor.ingested_byte_size(None, text) == len(text.encode("utf-8"))
    assert processor.ingested_byte_size(None, text) == 5  # c a f é(2)


@pytest.mark.unit
def test_build_point_vector_hybrid_carries_dense_and_sparse():
    """Hybrid mode (ADR-030) upserts both named vectors for the chunk."""
    dense = [[0.1, 0.2], [0.3, 0.4]]
    v = processor.build_point_vector("sparse-1", dense, 1, dense_enabled=True)
    assert v == {"sparse": "sparse-1", "dense": [0.3, 0.4]}


@pytest.mark.unit
def test_build_point_vector_keyword_is_sparse_only():
    """Keyword mode carries only the sparse vector and never indexes into the
    empty dense list (the silent zero-points bug guard)."""
    v = processor.build_point_vector("sparse-1", [], 0, dense_enabled=False)
    assert v == {"sparse": "sparse-1"}
    assert "dense" not in v


@pytest.mark.unit
def test_ingested_byte_size_mail_message_uses_utf8():
    """mail_message has no binary (content_bytes=None) → same UTF-8 arm as text."""
    # mail_message sets content_bytes=None in the processor, so it must measure
    # the extracted text, not be mistaken for a binary-backed (file) doc type.
    mail = "Subject: Hello\nBody text"
    assert processor.ingested_byte_size(None, mail) == len(mail.encode("utf-8"))


@pytest.mark.unit
async def test_parsed_file_records_pages_and_tokens(store_spy):
    """A parsed PDF fires all events: pages_embedded = real page count."""
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
        bytes_ingested=204800,
        bytes_stored=178000,
    )

    calls = store_spy.record_usage_event.await_args_list
    by_metric = {c.kwargs["metric"]: c.kwargs["value"] for c in calls}
    # pages_embedded is the real parsed-page count, NOT the chunk count;
    # bytes_* are the raw-ingest / stored-chunk byte counts (card #401).
    assert by_metric == {
        "tokens_embedded": 4242,
        "pages_embedded": 12,
        "bytes_ingested": 204800,
        "bytes_stored": 178000,
    }
    # Intentional ordering: tokens (recorded for every doc) before pages (the
    # conditional parsing cost). Asserted so a refactor can't silently reverse
    # it — a comment alone is easier to delete than a failing test. The byte
    # rows are appended after the pages block, so these two stay valid.
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
async def test_text_doc_records_tokens_and_bytes_no_pages(store_spy):
    """Unparsed text content (no page_count) meters tokens + bytes, never pages."""
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
        bytes_ingested=7100,
        bytes_stored=8200,
    )

    calls = store_spy.record_usage_event.await_args_list
    by_metric = {c.kwargs["metric"]: c.kwargs["value"] for c in calls}
    # Byte rows fire for text docs (they have no page_count, so no pages row).
    assert by_metric == {
        "tokens_embedded": 512,
        "bytes_ingested": 7100,
        "bytes_stored": 8200,
    }
    assert "pages_embedded" not in by_metric


@pytest.mark.unit
async def test_zero_pages_skips_pages(store_spy):
    """page_count=0 (e.g. an empty/corrupt PDF) records tokens+bytes but no pages."""
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
        bytes_ingested=2048,
        bytes_stored=1100,
    )

    calls = store_spy.record_usage_event.await_args_list
    by_metric = {c.kwargs["metric"]: c.kwargs["value"] for c in calls}
    assert by_metric == {
        "tokens_embedded": 99,
        "bytes_ingested": 2048,
        "bytes_stored": 1100,
    }


@pytest.mark.unit
async def test_negative_pages_skips_pages(store_spy):
    """A malformed negative page_count meters as 'no pages' (tokens+bytes only)."""
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
        bytes_ingested=2048,
        bytes_stored=1100,
    )

    calls = store_spy.record_usage_event.await_args_list
    by_metric = {c.kwargs["metric"]: c.kwargs["value"] for c in calls}
    assert by_metric == {
        "tokens_embedded": 99,
        "bytes_ingested": 2048,
        "bytes_stored": 1100,
    }


@pytest.mark.unit
async def test_nonpositive_bytes_skip_byte_rows(store_spy):
    """Zero/negative byte counts are skipped (no zero-value billing rows)."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="note",
        user_id="alice",
        chunk_count=4,
        token_count=99,
        total_chars=0,
        page_count=None,
        bytes_ingested=0,
        bytes_stored=-5,
    )

    calls = store_spy.record_usage_event.await_args_list
    by_metric = {c.kwargs["metric"]: c.kwargs["value"] for c in calls}
    # Only tokens_embedded survives — neither byte row is written.
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
        bytes_ingested=4096,
        bytes_stored=512,
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
        bytes_ingested=4096,
        bytes_stored=512,
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
        bytes_ingested=4096,
        bytes_stored=512,
    )


@pytest.mark.unit
async def test_ocr_tier_records_pages_ocr(store_spy):
    """OCR-tier pages are metered as a separate pages_ocr line (Deck #323)."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        chunk_count=20,
        token_count=900,
        total_chars=40000,
        page_count=8,
        bytes_ingested=51200,
        bytes_stored=41000,
        pipeline_tier="ocr",
    )
    by_metric = {
        c.kwargs["metric"]: c.kwargs["value"]
        for c in store_spy.record_usage_event.await_args_list
    }
    # pages_ocr fires IN ADDITION to pages_embedded for OCR-tier pages.
    assert by_metric == {
        "tokens_embedded": 900,
        "pages_embedded": 8,
        "pages_ocr": 8,
        "bytes_ingested": 51200,
        "bytes_stored": 41000,
    }
    # pipeline_tier is threaded into the billing metadata for CP attribution.
    for c in store_spy.record_usage_event.await_args_list:
        assert c.kwargs["metadata"]["pipeline_tier"] == "ocr"


@pytest.mark.unit
async def test_fast_tier_does_not_record_pages_ocr(store_spy):
    """A CPU-cheap fast-tier parse must NOT incur the paid pages_ocr line."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        chunk_count=10,
        token_count=500,
        total_chars=20000,
        page_count=4,
        bytes_ingested=25600,
        bytes_stored=20500,
        pipeline_tier="fast",
    )
    metrics = {c.kwargs["metric"] for c in store_spy.record_usage_event.await_args_list}
    assert "pages_ocr" not in metrics
    assert metrics == {
        "tokens_embedded",
        "pages_embedded",
        "bytes_ingested",
        "bytes_stored",
    }
