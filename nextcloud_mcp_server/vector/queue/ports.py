"""Ingest-path ports (design §10, hexagonal; Deck #183).

A ``TaskProducer`` is where the scanner + webhook receiver send a
``DocumentTask``. The transport behind it is swappable:

- ``MemoryTaskProducer`` over the in-process anyio ``MemoryObjectSendStream``
  (``INGEST_QUEUE=memory`` — the SQLite/dev default), and
- ``ProcrastinateTaskProducer`` (``INGEST_QUEUE=postgres``), which defers jobs
  into the per-tenant Postgres for the out-of-process ``worker`` role to drain.

The protocol is exactly the surface the scanner/oauth_sync already use on the
memory stream (``send`` + ``clone`` + ``async with``), so both adapters drop in
with only a type-annotation change at the call sites. There is no consumer port:
in memory mode the in-process processor pool is the consumer; in postgres mode
the procrastinate worker is.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..scanner import DocumentTask


@runtime_checkable
class TaskProducer(Protocol):
    """Sink for scanner/webhook ``DocumentTask``s (see module docstring)."""

    # Positional-only so anyio's MemoryObjectSendStream.send(item) structurally
    # satisfies this protocol (its parameter is named "item", not "task").
    async def send(self, task: DocumentTask, /) -> None: ...

    def clone(self) -> TaskProducer:
        """Return a producer handle for one user's scanner (multi-user mode).

        For the memory stream this is a real clone (each closed independently);
        for the bus it returns ``self`` (one shared connection).
        """
        ...

    async def __aenter__(self) -> TaskProducer: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def aclose(self) -> None:
        """Close *this* handle (e.g. a per-user clone when its scanner exits).

        For the memory stream this closes the clone; for a shared connection it
        is a no-op (the connection is owned by the lifespan, which tears it down
        once on shutdown).
        """
        ...

    # Note: this protocol deliberately omits ``drain()``. An implementation that
    # owns a long-lived shared connection (e.g. ProcrastinateTaskProducer's
    # connector pool) may additionally provide ``async def drain()`` for the
    # lifespan to close that pool once on shutdown; the lifespan probes for it
    # with ``getattr(task_producer, "drain", None)``, so it stays optional.
