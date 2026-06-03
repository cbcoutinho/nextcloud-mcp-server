"""Unit tests for the shared ingest-status read model (Deck #183)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nextcloud_mcp_server.vector.ingest_status import get_ingest_pending

pytestmark = pytest.mark.unit


class TestGetIngestPending:
    async def test_postgres_reads_job_counts(self):
        producer = AsyncMock()
        producer.job_counts.return_value = {"todo": 5, "doing": 2, "failed": 1}

        result = await get_ingest_pending(
            task_producer=producer,
            document_receive_stream=None,
            ingest_queue="postgres",
        )
        assert result.pending == 7  # todo + doing
        assert result.job_counts == {"todo": 5, "doing": 2, "failed": 1}

    async def test_postgres_degrades_to_zero_on_error(self):
        producer = AsyncMock()
        producer.job_counts.side_effect = RuntimeError("db down")

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
