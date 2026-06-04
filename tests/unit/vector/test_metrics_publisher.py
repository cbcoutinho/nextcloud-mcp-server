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
