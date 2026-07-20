"""Ingest-path transport (design §10, hexagonal; Deck #183 follow-up, ADR-028).

The :class:`TaskProducer` port (see ``ports.py``) is *one* side of ingest — the
sink the scanner/webhook send a ``DocumentTask`` to. An :class:`IngestTransport`
is the composition object that owns *both* sides of one backend:

- the ``producer`` to wire into ``app.state`` / hand to the scanner, and
- how the **consumer** side runs for *this* process.

Two adapters, selected by ``INGEST_QUEUE`` via :func:`build_transport`:

- :class:`LocalTransport` (``memory`` — the SQLite/dev default): an in-process
  anyio ``MemoryObjectStream`` drained by a pool of in-process workers that
  :meth:`run_consumers` starts.
- :class:`DistributedTransport` (``postgres``): wraps the
  :class:`ProcrastinateTaskProducer`; :meth:`run_consumers` is a no-op because
  the consumer is a *separate* process — the ``nextcloud-mcp-server worker``
  role drains the queue (see ``cli.py``).

There is deliberately no consumer *port* (mirroring ``ports.py``): in memory
mode the in-process pool is the consumer, in postgres mode the external worker
is. The transport just encapsulates "build the producer + run (or don't run)
the in-process consumers" so the server lifespan has a single branch-free shape
and a new backend (Redis/NATS/SQS) drops in as one more adapter + one
:func:`build_transport` arm — no ``app.py`` or scanner change.

Why an ABC here but a ``Protocol`` for ``TaskProducer``: the producer port is a
Protocol so anyio's third-party ``MemoryObjectSendStream`` satisfies it
structurally; the transport has exactly two in-house adapters that share the
``producer`` storage and the ``receive_stream``/``run_consumers``/``aclose``
defaults, so a concrete ABC is simpler and checks more cleanly under ``ty``.

The single-tenant parallelism invariant lives here: :class:`LocalTransport`
hands every worker a ``clone()`` of *one* shared receive stream, so a tenant's
users are processed by an N-worker pool off a single multiplexed queue
(per-document, not per-user, dispatch) — never one user fully then the next.
"""

from __future__ import annotations

import abc
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)

from .factory import build_producer
from .memory import MemoryTaskProducer

if TYPE_CHECKING:
    from ...config import Settings
    from ..scanner import DocumentTask
    from .ports import TaskProducer
    from .procrastinate import ProcrastinateTaskProducer

logger = logging.getLogger(__name__)


# A worker-spawn callback supplied by the lifespan: given a worker index and a
# *fresh* receive handle, start one in-process consumer in the lifespan's task
# group. The lifespan owns this closure so the transport never learns about auth
# modes (single shared nc_client+username vs per-document credential resolution
# by host). anyio's ``TaskGroup.start`` injects a ``task_status`` keyword, which
# the closure must forward to the underlying ``processor_task`` /
# ``multi_user_processor_task`` (else ``start`` blocks forever); hence ``...``.
# ``Coroutine`` (not the broader ``Awaitable``) because ``TaskGroup.start``
# accepts only coroutine functions, which every spawn closure already is.
SpawnWorker = Callable[..., Coroutine[Any, Any, None]]


class IngestTransport(abc.ABC):
    """Owns one ingest backend: the producer + running its in-process consumers.

    Built once per server lifespan by :func:`build_transport`. The
    :class:`TaskProducer` port is unchanged; this is a higher-level composition
    object owned only by the lifespan (the worker CLI talks to procrastinate
    directly — see module docstring).
    """

    @property
    @abc.abstractmethod
    def producer(self) -> TaskProducer:
        """The :class:`TaskProducer` to wire into ``app.state`` / the scanner."""

    @property
    @abc.abstractmethod
    def backend_name(self) -> str:
        """Short backend identifier for logs/metrics (``memory``/``postgres``).

        Lets the lifespan log which ingest backend is active without reading
        ``settings.ingest_queue`` — the backend choice stays inside the transport.
        """

    @property
    def send_stream(self) -> MemoryObjectSendStream[DocumentTask] | None:
        """Memory backend's raw send end; ``None`` for distributed backends.

        Exposed only so the lifespan can keep populating
        ``_vector_sync_state.document_send_stream`` (which the integration
        conftest saves/closes as a singleton). Producers send via
        :attr:`producer`, not this.
        """
        return None

    @property
    def receive_stream(self) -> MemoryObjectReceiveStream[DocumentTask] | None:
        """Memory backend's receive end (queue-depth surface); ``None`` for
        distributed backends, which have no in-process stream (``ingest_status``
        reads procrastinate job counts via the producer instead)."""
        return None

    @property
    def active_consumer_count(self) -> int:
        """In-process consumers started by :meth:`run_consumers` for this process.

        ``0`` by default — distributed backends run their consumers as a separate
        ``worker`` process. Lets the lifespan log the worker count without
        re-inspecting ``INGEST_QUEUE`` (keeping the backend choice inside the
        transport).
        """
        return 0

    async def run_consumers(
        self, task_group: TaskGroup, spawn_worker: SpawnWorker, count: int
    ) -> None:
        """Start the in-process consumer pool in ``task_group``.

        No-op by default: distributed backends are drained by the external
        ``worker`` role, so there is nothing to start in the API process.
        """
        return None

    async def aclose(self) -> None:
        """Tear down backend-owned resources once on lifespan shutdown.

        No-op by default; subclasses that own resources (a connector pool, stream
        handles) override this to release them — see
        :meth:`DistributedTransport.aclose` and :meth:`LocalTransport.aclose`.
        """
        return None


class LocalTransport(IngestTransport):
    """In-process anyio memory stream + processor pool (``INGEST_QUEUE=memory``).

    Builds the paired send/receive streams up front and owns the receive end;
    :meth:`run_consumers` hands each worker an independent ``clone()`` so every
    receiver observes end-of-stream when the scanner's send handles all close.
    """

    def __init__(self, max_buffer_size: float):
        # "DocumentTask" as a string (not the symbol): the class is
        # TYPE_CHECKING-only here, and anyio ignores the runtime value of the
        # type argument — so the string is intentional, not a typo.
        send_stream, receive_stream = anyio.create_memory_object_stream["DocumentTask"](
            max_buffer_size=max_buffer_size
        )
        self._send_stream = send_stream
        self._receive_stream = receive_stream
        self._producer = MemoryTaskProducer(send_stream)
        self._active_consumer_count = 0

    @property
    def producer(self) -> TaskProducer:
        return self._producer

    @property
    def backend_name(self) -> str:
        return "memory"

    @property
    def send_stream(self) -> MemoryObjectSendStream[DocumentTask]:
        return self._send_stream

    @property
    def receive_stream(self) -> MemoryObjectReceiveStream[DocumentTask]:
        return self._receive_stream

    @property
    def active_consumer_count(self) -> int:
        return self._active_consumer_count

    async def run_consumers(
        self, task_group: TaskGroup, spawn_worker: SpawnWorker, count: int
    ) -> None:
        # One shared receive stream, N workers each draining a clone → a single
        # multiplexed queue processed with N-way parallelism across all users in
        # the tenant (per-document dispatch). ``start`` (not ``start_soon``)
        # waits for each worker's ``task_status.started()`` readiness, matching
        # the prior inline lifespan behaviour.
        for i in range(count):
            await task_group.start(spawn_worker, i, self._receive_stream.clone())
            # Increment per-worker (not once after the loop) so the count is
            # accurate even if a later start() raises — a crash log then reflects
            # how many workers were actually live.
            self._active_consumer_count += 1

    async def aclose(self) -> None:
        # Belt-and-suspenders cleanup of the two stream ends this transport owns,
        # so they don't linger until GC (which can emit unclosed-resource
        # warnings under the test runner / alternative runtimes). anyio's aclose
        # is idempotent, so the scanner's own ``async with`` on the send side
        # (single-user) closing it first is harmless; worker receive *clones* are
        # independent handles, closed by task-group cancellation. By shutdown the
        # ``shutdown_event`` is already set, so the scanner is winding down rather
        # than issuing fresh sends.
        await self._send_stream.aclose()
        await self._receive_stream.aclose()


class DistributedTransport(IngestTransport):
    """Postgres/procrastinate producer; consumers are the external worker role.

    The producer's connector pool is opened by :func:`build_producer` and owned
    by the lifespan; :meth:`run_consumers` is the inherited no-op (the
    ``nextcloud-mcp-server worker`` process drains the queue) and :meth:`aclose`
    closes the pool once on shutdown.

    This adapter is postgres/procrastinate-specific by design: :meth:`aclose`
    calls ``ProcrastinateTaskProducer.drain()`` (the narrow ``_producer`` type
    confirms it). A different distributed backend (Redis/NATS/SQS) with its own
    shutdown semantics would be a separate :class:`IngestTransport` subclass, not
    a reconfiguration of this one.
    """

    def __init__(self, producer: ProcrastinateTaskProducer):
        # Explicit (not inferred): aclose() calls drain(), which lives on the
        # concrete ProcrastinateTaskProducer, not the TaskProducer protocol —
        # the annotation keeps that coupling visible and lets ty catch drift.
        self._producer: ProcrastinateTaskProducer = producer

    @property
    def producer(self) -> TaskProducer:
        return self._producer

    @property
    def backend_name(self) -> str:
        return "postgres"

    async def aclose(self) -> None:
        await self._producer.drain()


async def build_transport(settings: Settings) -> IngestTransport:
    """Build the ingest transport for the configured ``INGEST_QUEUE`` backend.

    - ``postgres`` → :class:`DistributedTransport`. Reuses :func:`build_producer`
      (which opens the connector pool) and applies procrastinate's schema once on
      that same open pool before any scanner can defer — a single open/close
      cycle, matching the ``worker`` command.
    - ``memory`` (SQLite/dev default) → :class:`LocalTransport`.
    """
    if settings.ingest_queue == "postgres":
        producer = await build_producer(settings)
        await producer.ensure_schema()
        logger.info("Ingest queue: postgres (procrastinate); worker drains it")
        return DistributedTransport(producer)

    logger.info("Ingest queue: memory (in-process anyio stream + processor pool)")
    return LocalTransport(max_buffer_size=settings.vector_sync_queue_max_size)
