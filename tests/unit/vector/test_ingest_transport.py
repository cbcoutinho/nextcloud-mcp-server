"""Unit tests for the ingest transport port (ADR-028; Deck #196).

Covers the factory's backend selection and each adapter's contract. The
single-tenant parallelism invariant (cross-user overlap) has its own follow-up
(Deck #197); here we only assert that ``LocalTransport.run_consumers`` starts the
requested number of workers off the shared stream.
"""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import anyio
import pytest
from anyio.abc import TaskGroup

import nextcloud_mcp_server.vector.queue.transport as transport_mod
from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.vector.queue import (
    DistributedTransport,
    LocalTransport,
    MemoryTaskProducer,
    SpawnWorker,
    build_transport,
)
from nextcloud_mcp_server.vector.scanner import DocumentTask

pytestmark = pytest.mark.unit


def _settings(**kwargs) -> Settings:
    """A duck-typed Settings carrying only the fields build_transport reads.

    cast keeps ``ty`` honest about the real signature while avoiding the cost of
    constructing a full Settings (dynaconf + validators) for a two-field read.
    """
    return cast(Settings, SimpleNamespace(**kwargs))


class TestBuildTransport:
    async def test_memory_returns_local_transport(self):
        settings = _settings(ingest_queue="memory", vector_sync_queue_max_size=7)
        transport = await build_transport(settings)

        assert isinstance(transport, LocalTransport)
        assert isinstance(transport.producer, MemoryTaskProducer)
        assert transport.backend_name == "memory"
        # Memory backend exposes both raw stream ends.
        assert transport.send_stream is not None
        assert transport.receive_stream is not None

    async def test_postgres_returns_distributed_transport(self, monkeypatch):
        producer = AsyncMock()

        async def fake_build_producer(settings):
            return producer

        monkeypatch.setattr(transport_mod, "build_producer", fake_build_producer)

        settings = _settings(ingest_queue="postgres")
        transport = await build_transport(settings)

        assert isinstance(transport, DistributedTransport)
        assert transport.producer is producer
        assert transport.backend_name == "postgres"
        # Schema applied once on the open pool before any defer.
        producer.ensure_schema.assert_awaited_once()
        # No in-process stream for the distributed backend.
        assert transport.send_stream is None
        assert transport.receive_stream is None


class TestLocalTransport:
    async def test_run_consumers_starts_count_workers_off_shared_stream(self):
        transport = LocalTransport(max_buffer_size=5)
        # Not yet started → no active consumers.
        assert transport.active_consumer_count == 0
        started: list[int] = []
        received_streams: list[object] = []

        async def fake_worker(
            worker_id, receive_stream, *, task_status=anyio.TASK_STATUS_IGNORED
        ):
            started.append(worker_id)
            received_streams.append(receive_stream)
            # Must signal readiness or tg.start blocks forever.
            task_status.started()

        async with anyio.create_task_group() as tg:
            await transport.run_consumers(tg, fake_worker, 3)

        assert sorted(started) == [0, 1, 2]
        assert transport.active_consumer_count == 3
        # Each worker gets its own distinct cloned receive handle (so each
        # observes end-of-stream when the senders all close), none None.
        assert len(received_streams) == 3
        assert all(s is not None for s in received_streams)
        assert len({id(s) for s in received_streams}) == 3

    async def test_aclose_closes_owned_streams_idempotently(self):
        transport = LocalTransport(max_buffer_size=5)
        await transport.aclose()

        # The send end is closed → the producer raises rather than silently
        # dropping (the producer wraps the same stream aclose() closed).
        with pytest.raises(anyio.ClosedResourceError):
            await transport.producer.send(
                DocumentTask(
                    user_id="u",
                    doc_id="1",
                    doc_type="note",
                    operation="index",
                    modified_at=0,
                )
            )
        # Idempotent: closing again is a no-op, not an error.
        await transport.aclose()


class TestDistributedTransport:
    async def test_run_consumers_is_noop(self):
        producer = AsyncMock()
        transport = DistributedTransport(producer)

        # No in-process consumers for the distributed backend.
        assert transport.active_consumer_count == 0

        # Passing sentinel task group / spawn callback proves the no-op never
        # touches them (the external worker is the consumer). The casts satisfy
        # the signature; the values are deliberately unusable to catch any
        # accidental use.
        sentinel_tg = cast(TaskGroup, object())
        sentinel_spawn = cast(SpawnWorker, None)
        await transport.run_consumers(sentinel_tg, sentinel_spawn, count=3)

        producer.assert_not_awaited()
        assert transport.active_consumer_count == 0

    async def test_aclose_drains_producer(self):
        producer = AsyncMock()
        transport = DistributedTransport(producer)

        await transport.aclose()

        producer.drain.assert_awaited_once()
