"""Unit tests for the indexing-path usage-metering helper (Deck #67 / #401).

``record_indexing_usage`` records the billable events after a document's chunks
are indexed: ``tokens_embedded`` for hybrid documents (skipped when
``token_count`` is 0, i.e. keyword-only docs), and ``pages_embedded`` only for
parsed files (real ``page_count``). Text content (no ``page_count``) meters
tokens only — ``pages_embedded`` is a charge for parsing, not content size (card
#282). The byte-volume dimensions ``bytes_ingested`` / ``bytes_stored`` (card
#401) are recorded for every document, skipped only on a non-positive count, and
carry an ``index_mode`` metadata dimension (card #609) so the CP rollup can slice
hybrid vs keyword ingestion. These cover the value mapping, the flag/zero-chunk
no-ops, the text-only path, and the best-effort failure path without standing up
the full document pipeline.

Since Deck #667 the helper writes a document's events with a single
``record_usage_events(events, enabled=True)`` batch call (one connection + one
commit) rather than N ``record_usage_event`` calls, so these tests assert on the
batched ``UsageEvent`` list.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.vector import processor


@pytest.fixture
def store_spy(monkeypatch):
    """Patch UsageEventStore.shared() to return a spy store."""
    store = MagicMock()
    store.record_usage_events = AsyncMock()
    monkeypatch.setattr(
        processor.UsageEventStore, "shared", AsyncMock(return_value=store)
    )
    return store


def _events(store_spy):
    """The ``UsageEvent`` list from the single batched record_usage_events() call."""
    store_spy.record_usage_events.assert_awaited_once()
    return list(store_spy.record_usage_events.await_args.args[0])


@pytest.mark.unit
def test_ingested_byte_size_files_use_raw_binary():
    """A file's bytes_ingested is its raw binary size, not the text size.

    Takes the size rather than the bytes: a streamed document is never fully
    resident, so the caller reads it off the document source.
    """
    raw_size = len(b"%PDF-1.7 ...binary..." + b"\x00" * 500)
    assert processor.ingested_byte_size(raw_size, "short extracted text") == raw_size


@pytest.mark.unit
def test_ingested_byte_size_text_uses_utf8_length():
    """Text doc types (no source size) fall back to UTF-8 byte length."""
    # Multibyte char: 1 code point, 2 UTF-8 bytes — len(str) would undercount.
    text = "café"
    assert processor.ingested_byte_size(None, text) == len(text.encode("utf-8"))
    assert processor.ingested_byte_size(None, text) == 5  # c a f é(2)


@pytest.mark.unit
def test_build_point_vector_hybrid_carries_dense_and_sparse():
    """A hybrid document upserts both named vectors for the chunk."""
    dense = [[0.1, 0.2], [0.3, 0.4]]
    v = processor.build_point_vector("sparse-1", dense, 1, dense_enabled=True)
    assert v == {"sparse": "sparse-1", "dense": [0.3, 0.4]}


@pytest.mark.unit
def test_build_point_vector_keyword_is_sparse_only():
    """A keyword document carries only the sparse vector and never indexes into
    the empty dense list (the silent zero-points bug guard)."""
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
        index_mode="hybrid",
        chunk_count=110,
        token_count=4242,
        total_chars=170826,
        page_count=12,
        bytes_ingested=204800,
        bytes_stored=178000,
    )

    events = _events(store_spy)
    by_metric = {e.metric: e.value for e in events}
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
    assert events[0].metric == "tokens_embedded"
    assert events[1].metric == "pages_embedded"
    # One batched write, hot-path fast-gated (the store skips a Settings rebuild).
    assert store_spy.record_usage_events.await_args.kwargs["enabled"] is True
    for e in events:
        # Tenant-local attribution metadata, threaded onto every event.
        assert e.metadata["provider"] == "mistral"
        assert e.metadata["model"] == "mistral-embed"
        assert e.metadata["user_id"] == "alice"
        assert e.metadata["doc_type"] == "file"
        # index_mode lets the CP rollup slice hybrid vs keyword ingestion (#609).
        assert e.metadata["index_mode"] == "hybrid"


@pytest.mark.unit
async def test_text_doc_records_tokens_and_bytes_no_pages(store_spy):
    """Unparsed text content (no page_count) meters tokens + bytes, never pages."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="note",
        user_id="alice",
        index_mode="hybrid",
        chunk_count=4,
        token_count=512,
        total_chars=7000,
        page_count=None,
        bytes_ingested=7100,
        bytes_stored=8200,
    )

    by_metric = {e.metric: e.value for e in _events(store_spy)}
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
        index_mode="hybrid",
        chunk_count=4,
        token_count=99,
        total_chars=1000,
        page_count=0,
        bytes_ingested=2048,
        bytes_stored=1100,
    )

    by_metric = {e.metric: e.value for e in _events(store_spy)}
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
        index_mode="hybrid",
        chunk_count=4,
        token_count=99,
        total_chars=1000,
        page_count=-1,
        bytes_ingested=2048,
        bytes_stored=1100,
    )

    by_metric = {e.metric: e.value for e in _events(store_spy)}
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
        index_mode="hybrid",
        chunk_count=4,
        token_count=99,
        total_chars=0,
        page_count=None,
        bytes_ingested=0,
        bytes_stored=-5,
    )

    by_metric = {e.metric: e.value for e in _events(store_spy)}
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
        index_mode="hybrid",
        chunk_count=10,
        token_count=20,
        total_chars=5,
        page_count=3,
        bytes_ingested=4096,
        bytes_stored=512,
    )
    store_spy.record_usage_events.assert_not_awaited()


@pytest.mark.unit
async def test_zero_chunks_is_noop(store_spy):
    """A document with no chunks records nothing (no zero-value rows)."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        index_mode="hybrid",
        chunk_count=0,
        token_count=0,
        total_chars=0,
        page_count=3,
        bytes_ingested=4096,
        bytes_stored=512,
    )
    store_spy.record_usage_events.assert_not_awaited()


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
        index_mode="hybrid",
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
        index_mode="hybrid",
        chunk_count=20,
        token_count=900,
        total_chars=40000,
        page_count=8,
        bytes_ingested=51200,
        bytes_stored=41000,
        pipeline_tier="ocr",
    )
    events = _events(store_spy)
    by_metric = {e.metric: e.value for e in events}
    # pages_ocr fires IN ADDITION to pages_embedded for OCR-tier pages.
    assert by_metric == {
        "tokens_embedded": 900,
        "pages_embedded": 8,
        "pages_ocr": 8,
        "bytes_ingested": 51200,
        "bytes_stored": 41000,
    }
    # pipeline_tier is threaded into the billing metadata for CP attribution.
    for e in events:
        assert e.metadata["pipeline_tier"] == "ocr"


@pytest.mark.unit
async def test_fast_tier_does_not_record_pages_ocr(store_spy):
    """A CPU-cheap fast-tier parse must NOT incur the paid pages_ocr line."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        index_mode="hybrid",
        chunk_count=10,
        token_count=500,
        total_chars=20000,
        page_count=4,
        bytes_ingested=25600,
        bytes_stored=20500,
        pipeline_tier="fast",
    )
    metrics = {e.metric for e in _events(store_spy)}
    assert "pages_ocr" not in metrics
    assert metrics == {
        "tokens_embedded",
        "pages_embedded",
        "bytes_ingested",
        "bytes_stored",
    }


@pytest.mark.unit
async def test_keyword_mode_meters_bytes_not_tokens(store_spy):
    """A keyword-only file (index_mode='keyword', token_count=0) meters the byte
    dimensions tagged index_mode='keyword' but emits NO tokens_embedded row —
    keyword docs never embed, so there is no embedding-token cost (card #609)."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="file",
        user_id="alice",
        index_mode="keyword",
        chunk_count=12,
        token_count=0,  # keyword docs skip dense embedding → zero embed tokens
        total_chars=30000,
        page_count=5,
        bytes_ingested=40960,
        bytes_stored=30500,
    )

    events = _events(store_spy)
    by_metric = {e.metric: e.value for e in events}
    # No tokens_embedded row; parsing (pages) + byte volume still bill.
    assert by_metric == {
        "pages_embedded": 5,
        "bytes_ingested": 40960,
        "bytes_stored": 30500,
    }
    assert "tokens_embedded" not in by_metric
    # Every event carries the keyword mode so the CP can attribute it separately.
    for e in events:
        assert e.metadata["index_mode"] == "keyword"


@pytest.mark.unit
async def test_hybrid_zero_tokens_still_skips_tokens_row(store_spy):
    """Guard the token gate independently of mode: a 0 token_count never writes a
    tokens_embedded row even for a hybrid doc (defensive — a real hybrid embed
    always reports > 0, but a zero must not emit a bogus billing row)."""
    await processor.record_indexing_usage(
        enabled=True,
        provider="mistral",
        model="mistral-embed",
        doc_type="note",
        user_id="alice",
        index_mode="hybrid",
        chunk_count=3,
        token_count=0,
        total_chars=100,
        page_count=None,
        bytes_ingested=200,
        bytes_stored=250,
    )
    metrics = {e.metric for e in _events(store_spy)}
    assert "tokens_embedded" not in metrics
    assert metrics == {"bytes_ingested", "bytes_stored"}
