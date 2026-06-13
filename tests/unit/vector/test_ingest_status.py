"""Unit tests for the shared ingest-status read model (Deck #183)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nextcloud_mcp_server.vector.ingest_status import get_ingest_pending

pytestmark = pytest.mark.unit


class TestGetIngestPending:
    async def test_postgres_aggregates_by_queue(self):
        """Per-queue counts are summed into fleet-wide totals (Deck #323)."""
        producer = AsyncMock()
        producer.job_counts_by_queue.return_value = {
            "ingest-fast": {"todo": 5, "doing": 1},
            "ingest-ocr": {"doing": 1, "failed": 1},
        }

        result = await get_ingest_pending(
            task_producer=producer,
            document_receive_stream=None,
            ingest_queue="postgres",
        )
        assert result.pending == 7  # todo(5) + doing(1+1)
        assert result.job_counts == {"todo": 5, "doing": 2, "failed": 1}
        assert result.job_counts_by_queue == {
            "ingest-fast": {"todo": 5, "doing": 1},
            "ingest-ocr": {"doing": 1, "failed": 1},
        }

    async def test_postgres_falls_back_to_aggregate_counts(self):
        """A producer without job_counts_by_queue uses the aggregated call."""

        class LegacyProducer:
            async def job_counts(self):
                return {"todo": 5, "doing": 2, "failed": 1}

        result = await get_ingest_pending(
            task_producer=LegacyProducer(),
            document_receive_stream=None,
            ingest_queue="postgres",
        )
        assert result.pending == 7
        assert result.job_counts == {"todo": 5, "doing": 2, "failed": 1}
        assert result.job_counts_by_queue is None

    async def test_postgres_degrades_to_zero_on_error(self):
        producer = AsyncMock()
        producer.job_counts_by_queue.side_effect = RuntimeError("db down")

        result = await get_ingest_pending(
            task_producer=producer,
            document_receive_stream=None,
            ingest_queue="postgres",
        )
        assert result.pending == 0
        assert result.job_counts == {}

    async def test_memory_reads_stream_buffer(self):
        stream = SimpleNamespace(
            statistics=lambda: SimpleNamespace(current_buffer_used=3)
        )
        result = await get_ingest_pending(
            task_producer=None,
            document_receive_stream=stream,
            ingest_queue="memory",
        )
        assert result.pending == 3
        assert result.job_counts is None

    async def test_memory_without_stream_is_zero(self):
        result = await get_ingest_pending(
            task_producer=None,
            document_receive_stream=None,
            ingest_queue="memory",
        )
        assert result.pending == 0
        assert result.job_counts is None
