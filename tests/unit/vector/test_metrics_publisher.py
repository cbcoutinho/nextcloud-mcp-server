"""Unit tests for the vector-sync metrics publisher.

Covers vector/metrics_publisher.py: the exact document/chunk counting
(documents via the chunk_index=0 point) and the fail-safe snapshot publisher
that fixes the queue gauge reading 0 on the multi-user consumer path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from nextcloud_mcp_server.vector import metrics_publisher as mp
from nextcloud_mcp_server.vector.ingest_status import IngestPending

pytestmark = pytest.mark.unit

_COLLECTION = "test_collection"


def _count_obj(n: int) -> SimpleNamespace:
    return SimpleNamespace(count=n)


def _must_keys(flt) -> list[str | None]:
    return [getattr(c, "key", None) for c in (flt.must or [])]


class TestCountIndexed:
    async def test_returns_documents_and_chunks(self) -> None:
        qc = AsyncMock()
        # count() is called for chunks first, then documents.
        qc.count.side_effect = [_count_obj(16039), _count_obj(486)]

        documents, chunks = await mp.count_indexed(qc, _COLLECTION)

        assert (documents, chunks) == (486, 16039)
        assert qc.count.await_count == 2

    async def test_document_count_filters_on_chunk_index_zero(self) -> None:
        qc = AsyncMock()
        qc.count.side_effect = [_count_obj(10), _count_obj(3)]

        await mp.count_indexed(qc, _COLLECTION)

        # First call = chunks (placeholder filter only); second = documents
        # (placeholder filter + chunk_index), the distinct-document trick.
        chunks_filter = qc.count.await_args_list[0].kwargs["count_filter"]
        docs_filter = qc.count.await_args_list[1].kwargs["count_filter"]
        assert _must_keys(chunks_filter) == ["is_placeholder"]
        assert _must_keys(docs_filter) == ["is_placeholder", "chunk_index"]

    async def test_placeholder_filter_excludes_placeholders(self) -> None:
        # is_placeholder must match False (exclude), not True (which would count
        # the in-flight placeholders as if they were indexed content).
        qc = AsyncMock()
        qc.count.side_effect = [_count_obj(10), _count_obj(3)]

        await mp.count_indexed(qc, _COLLECTION)

        chunks_filter = qc.count.await_args_list[0].kwargs["count_filter"]
        assert chunks_filter.must[0].match.value is False
        docs_filter = qc.count.await_args_list[1].kwargs["count_filter"]
        # And the distinct-document filter pins chunk_index to 0.
        assert docs_filter.must[0].match.value is False
        assert docs_filter.must[1].match.value == 0

    async def test_exact_kwarg_forwarded(self) -> None:
        # The gauge path passes exact=False; dropping it would silently make the
        # every-N-seconds refresh do exact counts on large tenants.
        qc = AsyncMock()
        qc.count.side_effect = [_count_obj(10), _count_obj(3)]

        await mp.count_indexed(qc, _COLLECTION, exact=False)

        assert all(call.kwargs["exact"] is False for call in qc.count.await_args_list)

    async def test_default_is_exact_true(self) -> None:
        # The on-demand status endpoint relies on the exact=True default.
        qc = AsyncMock()
        qc.count.side_effect = [_count_obj(10), _count_obj(3)]

        await mp.count_indexed(qc, _COLLECTION)

        assert all(call.kwargs["exact"] is True for call in qc.count.await_args_list)


class TestCountHybridChunks:
    async def test_counts_hybrid_only(self) -> None:
        qc = AsyncMock()
        qc.count.return_value = _count_obj(1234)

        hybrid = await mp.count_hybrid_chunks(qc, _COLLECTION)

        assert hybrid == 1234
        assert qc.count.await_count == 1

    async def test_filters_on_placeholder_and_hybrid_index_mode(self) -> None:
        qc = AsyncMock()
        qc.count.return_value = _count_obj(3)

        await mp.count_hybrid_chunks(qc, _COLLECTION)

        flt = qc.count.await_args.kwargs["count_filter"]
        # Excludes placeholders AND restricts to the hybrid (dense-bearing) mode.
        assert _must_keys(flt) == ["is_placeholder", "index_mode"]
        assert flt.must[0].match.value is False
        assert flt.must[1].match.value == "hybrid"

    async def test_exact_kwarg_forwarded(self) -> None:
        qc = AsyncMock()
        qc.count.return_value = _count_obj(3)

        await mp.count_hybrid_chunks(qc, _COLLECTION, exact=False)

        assert qc.count.await_args.kwargs["exact"] is False


class TestEstimateHybridVectorBytes:
    """Shared helper used by the MCP tool + HTTP status route (no drift)."""

    async def test_returns_hybrid_count_and_estimate(self, monkeypatch) -> None:
        qc = AsyncMock()
        qc.count.return_value = _count_obj(600)
        monkeypatch.setattr(
            mp,
            "get_embedding_service",
            lambda: SimpleNamespace(get_dimension=lambda: 1024),
        )

        hybrid, estimated = await mp.estimate_hybrid_vector_bytes(qc, _COLLECTION, 1.5)

        assert hybrid == 600
        # 600 * 1024 * 4 * 1.5, rounded to int for the response payload.
        assert estimated == int(600 * 1024 * 4 * 1.5)


class TestPublishVectorRamGauges:
    """The dense-vector RAM gauges block of publish_vector_sync_metrics (#624)."""

    @pytest.fixture(autouse=True)
    def _stub_settings(self, monkeypatch) -> None:
        settings = SimpleNamespace(
            ingest_queue="memory",
            get_collection_name=lambda: _COLLECTION,
            vector_ram_hnsw_overhead_factor=1.5,
        )
        monkeypatch.setattr(mp, "get_settings", lambda: settings)
        monkeypatch.setattr(
            mp, "get_ingest_pending", AsyncMock(return_value=IngestPending(pending=0))
        )
        # Dimension is a fixed 1024 for the estimate math.
        monkeypatch.setattr(
            mp,
            "get_embedding_service",
            lambda: SimpleNamespace(get_dimension=lambda: 1024),
        )

    @pytest.fixture
    def ram_gauges(self, monkeypatch) -> dict[str, MagicMock]:
        g = {
            name: MagicMock()
            for name in (
                "update_vector_sync_estimated_vector_bytes",
                "update_vector_sync_qdrant_vectors",
                "update_vector_sync_qdrant_vector_bytes",
            )
        }
        for name, mock in g.items():
            monkeypatch.setattr(mp, name, mock)
        return g

    async def test_publishes_estimate_and_qdrant_actuals(
        self, monkeypatch, ram_gauges
    ) -> None:
        qc = AsyncMock()
        # count_indexed (2 calls) then count_hybrid_chunks (1 call).
        qc.count.side_effect = [_count_obj(1000), _count_obj(200), _count_obj(600)]
        qc.get_collection.return_value = SimpleNamespace(
            vectors_count=700, points_count=1000
        )
        monkeypatch.setattr(mp, "get_qdrant_client", AsyncMock(return_value=qc))

        await mp.publish_vector_sync_metrics(
            task_producer=None, document_receive_stream=object()
        )

        # Estimate uses OUR hybrid count (600): 600 * 1024 * 4 * 1.5.
        ram_gauges["update_vector_sync_estimated_vector_bytes"].assert_called_once_with(
            600 * 1024 * 4 * 1.5
        )
        # Qdrant actuals use the reported vectors_count (700).
        ram_gauges["update_vector_sync_qdrant_vectors"].assert_called_once_with(700)
        ram_gauges["update_vector_sync_qdrant_vector_bytes"].assert_called_once_with(
            700 * 1024 * 4 * 1.5
        )

    async def test_falls_back_to_points_count_when_vectors_count_none(
        self, monkeypatch, ram_gauges
    ) -> None:
        qc = AsyncMock()
        qc.count.side_effect = [_count_obj(1000), _count_obj(200), _count_obj(600)]
        qc.get_collection.return_value = SimpleNamespace(
            vectors_count=None, points_count=999
        )
        monkeypatch.setattr(mp, "get_qdrant_client", AsyncMock(return_value=qc))

        await mp.publish_vector_sync_metrics(
            task_producer=None, document_receive_stream=object()
        )

        ram_gauges["update_vector_sync_qdrant_vectors"].assert_called_once_with(999)

    async def test_qdrant_failure_does_not_raise(self, monkeypatch, ram_gauges) -> None:
        # get_collection raising must be swallowed — metrics can't disturb ingest.
        qc = AsyncMock()
        qc.count.side_effect = [_count_obj(1000), _count_obj(200), _count_obj(600)]
        qc.get_collection.side_effect = RuntimeError("qdrant down")
        monkeypatch.setattr(mp, "get_qdrant_client", AsyncMock(return_value=qc))

        await mp.publish_vector_sync_metrics(
            task_producer=None, document_receive_stream=object()
        )

        # The estimate gauge (computed before get_collection) still published;
        # the Qdrant-actuals gauges did not (the exception short-circuited them).
        ram_gauges["update_vector_sync_estimated_vector_bytes"].assert_called_once()
        ram_gauges["update_vector_sync_qdrant_vectors"].assert_not_called()


class TestPublishVectorSyncMetrics:
    @pytest.fixture(autouse=True)
    def _stub_settings(self, monkeypatch) -> None:
        settings = SimpleNamespace(
            ingest_queue="memory",
            get_collection_name=lambda: _COLLECTION,
        )
        monkeypatch.setattr(mp, "get_settings", lambda: settings)

    @pytest.fixture
    def gauges(self, monkeypatch) -> dict[str, MagicMock]:
        g = {
            name: MagicMock()
            for name in (
                "update_vector_sync_pending_documents",
                "update_vector_sync_queue_size",
                "update_vector_sync_indexed_documents",
                "update_vector_sync_indexed_chunks",
            )
        }
        for name, mock in g.items():
            monkeypatch.setattr(mp, name, mock)
        return g

    async def test_publishes_all_gauges(self, monkeypatch, gauges) -> None:
        monkeypatch.setattr(
            mp,
            "get_ingest_pending",
            AsyncMock(return_value=IngestPending(pending=2214)),
        )
        qc = AsyncMock()
        qc.count.side_effect = [_count_obj(16039), _count_obj(486)]
        monkeypatch.setattr(mp, "get_qdrant_client", AsyncMock(return_value=qc))

        await mp.publish_vector_sync_metrics(
            task_producer=None, document_receive_stream=object()
        )

        gauges["update_vector_sync_pending_documents"].assert_called_once_with(2214)
        # Legacy gauge kept meaningful on every consumer path.
        gauges["update_vector_sync_queue_size"].assert_called_once_with(2214)
        gauges["update_vector_sync_indexed_documents"].assert_called_once_with(486)
        gauges["update_vector_sync_indexed_chunks"].assert_called_once_with(16039)

    async def test_pending_failure_does_not_block_corpus_gauges(
        self, monkeypatch, gauges
    ) -> None:
        # get_ingest_pending raising must not stop the indexed gauges (and must
        # not propagate — a metrics refresh cannot disturb ingest).
        monkeypatch.setattr(
            mp, "get_ingest_pending", AsyncMock(side_effect=RuntimeError("queue down"))
        )
        qc = AsyncMock()
        qc.count.side_effect = [_count_obj(10), _count_obj(3)]
        monkeypatch.setattr(mp, "get_qdrant_client", AsyncMock(return_value=qc))

        await mp.publish_vector_sync_metrics(
            task_producer=None, document_receive_stream=object()
        )

        gauges["update_vector_sync_pending_documents"].assert_not_called()
        gauges["update_vector_sync_indexed_documents"].assert_called_once_with(3)
        gauges["update_vector_sync_indexed_chunks"].assert_called_once_with(10)

    async def test_qdrant_failure_does_not_block_pending_gauge(
        self, monkeypatch, gauges
    ) -> None:
        monkeypatch.setattr(
            mp,
            "get_ingest_pending",
            AsyncMock(return_value=IngestPending(pending=42)),
        )
        monkeypatch.setattr(
            mp, "get_qdrant_client", AsyncMock(side_effect=RuntimeError("qdrant down"))
        )

        await mp.publish_vector_sync_metrics(
            task_producer=None, document_receive_stream=object()
        )

        gauges["update_vector_sync_pending_documents"].assert_called_once_with(42)
        gauges["update_vector_sync_indexed_documents"].assert_not_called()


class TestVectorSyncMetricsTask:
    async def test_publishes_then_exits_on_shutdown(self, monkeypatch) -> None:
        shutdown = anyio.Event()
        published = 0

        async def _fake_publish(task_producer, document_receive_stream) -> None:
            nonlocal published
            published += 1
            shutdown.set()  # one pass, then stop the loop

        monkeypatch.setattr(
            mp, "publish_vector_sync_metrics", AsyncMock(side_effect=_fake_publish)
        )
        monkeypatch.setattr(
            mp,
            "get_settings",
            lambda: SimpleNamespace(vector_sync_metrics_refresh_interval=0),
        )

        await mp.vector_sync_metrics_task(None, None, shutdown)

        assert published == 1


def _point(**payload) -> SimpleNamespace:
    """A fake Qdrant scroll Record carrying only a payload."""
    return SimpleNamespace(payload=payload)


def _scroll_returning(*pages):
    """Build an AsyncMock scroll returning ``(points, next_offset)`` per page.

    Each ``pages`` entry is a list of points; the offset is a sentinel for every
    page except the last, which returns ``None`` to end the scroll.
    """
    results = []
    for i, points in enumerate(pages):
        offset = None if i == len(pages) - 1 else f"offset-{i}"
        results.append((points, offset))
    return AsyncMock(side_effect=results)


class TestComputeChunkDensitySnapshot:
    async def test_buckets_gsum_and_uncovered(self) -> None:
        qc = AsyncMock()
        qc.scroll = _scroll_returning(
            [
                _point(doc_type="note", total_chunks=3, source_bytes=1_000_000),
                _point(doc_type="note", total_chunks=100, source_bytes=1_000_000),
                _point(doc_type="deck_card", total_chunks=1, source_bytes=2_000_000),
                # No source_bytes -> uncovered.
                _point(doc_type="file", total_chunks=10),
                # Non-positive source_bytes -> uncovered.
                _point(doc_type="file", total_chunks=5, source_bytes=0),
            ],
        )

        (
            per_doc_type,
            uncovered,
            truncated,
            source_bytes,
        ) = await mp.compute_chunk_density_snapshot(qc, _COLLECTION, max_documents=1000)

        assert truncated is False
        assert uncovered == {"file": 2}

        # note: density 3 -> idx1 (le=5), density 100 -> idx7 (le=120); gsum=103.
        note_counts, note_gsum = per_doc_type["note"]
        assert note_counts[1] == 1
        assert note_counts[7] == 1
        assert note_gsum == pytest.approx(103.0)
        assert sum(note_counts) == 2  # only the two covered notes

        # deck_card: density 0.5 -> idx0 (le=1); gsum=0.5.
        deck_counts, deck_gsum = per_doc_type["deck_card"]
        assert deck_counts[0] == 1
        assert deck_gsum == pytest.approx(0.5)

        # source_bytes_totals sums the SAME covered docs (byte-weighted denominator):
        # two notes @1e6 -> 2e6, one deck_card @2e6 -> 2e6. The two `file` docs are
        # uncovered (missing / non-positive source_bytes) so they contribute nothing
        # and `file` has no series at all — matching the uncovered semantics.
        assert source_bytes["note"] == pytest.approx(2_000_000.0)
        assert source_bytes["deck_card"] == pytest.approx(2_000_000.0)
        assert "file" not in source_bytes

    async def test_scrolls_chunk_index_zero_non_placeholder(self) -> None:
        qc = AsyncMock()
        qc.scroll = _scroll_returning([])

        await mp.compute_chunk_density_snapshot(qc, _COLLECTION, max_documents=1000)

        kwargs = qc.scroll.await_args_list[0].kwargs
        assert kwargs["with_vectors"] is False
        assert "source_bytes" in kwargs["with_payload"]
        # One point per document (chunk_index=0), placeholders excluded.
        assert _must_keys(kwargs["scroll_filter"]) == ["is_placeholder", "chunk_index"]

    async def test_paginates_until_offset_none(self) -> None:
        qc = AsyncMock()
        qc.scroll = _scroll_returning(
            [_point(doc_type="note", total_chunks=3, source_bytes=1_000_000)],
            [_point(doc_type="note", total_chunks=3, source_bytes=1_000_000)],
        )

        (
            per_doc_type,
            _,
            truncated,
            source_bytes,
        ) = await mp.compute_chunk_density_snapshot(qc, _COLLECTION, max_documents=1000)

        assert qc.scroll.await_count == 2
        assert truncated is False
        assert per_doc_type["note"][0][1] == 2  # both notes counted in le=5 slot
        # source_bytes accumulates across pages: 1e6 + 1e6.
        assert source_bytes["note"] == pytest.approx(2_000_000.0)

    async def test_truncates_when_corpus_strictly_exceeds_cap(self) -> None:
        qc = AsyncMock()

        def note():
            return _point(doc_type="note", total_chunks=3, source_bytes=1_000_000)

        # cap=2, pages of 2: after page 2 scanned=4 (> cap) with a further page
        # still to come -> genuinely truncated, stop before fetching page 3.
        qc.scroll = _scroll_returning(
            [note(), note()],  # scanned=2 (== cap, NOT yet truncated)
            [note(), note()],  # scanned=4 (> cap) -> truncated
            [note()],  # never fetched
        )

        _, _, truncated, _ = await mp.compute_chunk_density_snapshot(
            qc, _COLLECTION, max_documents=2, page_size=2
        )

        assert truncated is True
        assert qc.scroll.await_count == 2  # third page never fetched

    async def test_exact_cap_boundary_is_not_truncated(self) -> None:
        # Regression for the false-positive flagged in review: Qdrant can return a
        # non-None next offset even at the exact end, so a collection sized exactly
        # at the cap must NOT be reported as truncated. Model that: a full page at
        # the cap with a non-None offset, then an empty page with offset=None.
        qc = AsyncMock()
        qc.scroll = _scroll_returning(
            [
                _point(doc_type="note", total_chunks=3, source_bytes=1_000_000),
                _point(doc_type="note", total_chunks=3, source_bytes=1_000_000),
            ],
            [],  # empty trailing page, offset=None -> authoritative end
        )

        _, _, truncated, _ = await mp.compute_chunk_density_snapshot(
            qc, _COLLECTION, max_documents=2, page_size=2
        )

        assert truncated is False


class TestPublishChunkDensitySnapshot:
    async def test_computes_and_publishes(self, monkeypatch) -> None:
        monkeypatch.setattr(
            mp,
            "get_settings",
            lambda: SimpleNamespace(
                vector_density_snapshot_max_documents=1000,
                get_collection_name=lambda: _COLLECTION,
            ),
        )
        qc = AsyncMock()
        qc.scroll = _scroll_returning(
            [_point(doc_type="note", total_chunks=3, source_bytes=1_000_000)],
        )
        monkeypatch.setattr(mp, "get_qdrant_client", AsyncMock(return_value=qc))
        published = MagicMock()
        monkeypatch.setattr(mp, "update_qdrant_chunk_density_snapshot", published)

        await mp.publish_chunk_density_snapshot()

        published.assert_called_once()
        _, kwargs = published.call_args
        assert kwargs["truncated"] is False
        assert kwargs["uncovered"] == {}
        # The byte-weighted denominator is threaded through to the publish entry.
        assert kwargs["source_bytes"] == {"note": pytest.approx(1_000_000.0)}

    async def test_qdrant_failure_is_swallowed(self, monkeypatch) -> None:
        monkeypatch.setattr(
            mp,
            "get_settings",
            lambda: SimpleNamespace(
                vector_density_snapshot_max_documents=1000,
                get_collection_name=lambda: _COLLECTION,
            ),
        )
        monkeypatch.setattr(
            mp, "get_qdrant_client", AsyncMock(side_effect=RuntimeError("qdrant down"))
        )
        published = MagicMock()
        monkeypatch.setattr(mp, "update_qdrant_chunk_density_snapshot", published)

        # Must not raise — a metrics refresh cannot disturb ingest.
        await mp.publish_chunk_density_snapshot()

        published.assert_not_called()


class TestVectorDensitySnapshotTask:
    async def test_publishes_then_exits_on_shutdown(self, monkeypatch) -> None:
        shutdown = anyio.Event()
        published = 0

        # Plain callable: AsyncMock still awaits fine, and this avoids an
        # async-def-without-await that the analyzer flags.
        def _fake_publish() -> None:
            nonlocal published
            published += 1
            shutdown.set()

        monkeypatch.setattr(
            mp, "publish_chunk_density_snapshot", AsyncMock(side_effect=_fake_publish)
        )
        monkeypatch.setattr(
            mp,
            "get_settings",
            lambda: SimpleNamespace(vector_density_snapshot_interval=0),
        )

        await mp.vector_density_snapshot_task(shutdown)

        assert published == 1
