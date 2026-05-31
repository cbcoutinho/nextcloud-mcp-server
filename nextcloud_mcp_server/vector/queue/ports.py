"""Ingest-path ports (design §10, hexagonal).

A ``TaskProducer`` is where the scanner + webhook receiver send a
``DocumentTask``. The transport behind it is swappable:

- the in-process anyio ``MemoryObjectSendStream`` (local ingest — the default),
- ``NatsTaskProducer`` (external ingest → the document-processor), and
- a future Postgres-queue producer (seam only; the *external* processor owns the
  consume side — see ``postgres.py``).

The protocol is exactly the surface the scanner/oauth_sync already use on the
memory stream (``send`` + ``clone`` + ``async with``), so both adapters drop in
with only a type-annotation change at the call sites. There is intentionally NO
consumer port: the MCP server's only in-process consumer is the memory stream;
when ingest is external the document-processor is the consumer, not this server.
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

        For the memory stream this closes the clone; for the shared bus
        connection it is a no-op (the connection is owned by the lifespan,
        which drains it once on shutdown).
        """
        ...
