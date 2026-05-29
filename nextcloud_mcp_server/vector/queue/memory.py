"""In-memory ``TaskProducer`` — the default (local) ingest transport.

A thin adapter over anyio's ``MemoryObjectSendStream`` so the local path is an
explicit :class:`TaskProducer` (rather than relying on structural typing of a
third-party class). Semantics are identical to using the stream directly:
``send`` enqueues, ``clone`` yields an independent per-user handle, ``async
with`` / ``aclose`` close the (cloned) send end so the processor pool's
receivers observe end-of-stream.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING

from anyio.streams.memory import MemoryObjectSendStream

if TYPE_CHECKING:
    from ..scanner import DocumentTask


class MemoryTaskProducer:
    def __init__(self, stream: MemoryObjectSendStream[DocumentTask]):
        self._stream = stream

    async def send(self, task: DocumentTask, /) -> None:
        await self._stream.send(task)

    def clone(self) -> MemoryTaskProducer:
        return MemoryTaskProducer(self._stream.clone())

    async def __aenter__(self) -> MemoryTaskProducer:
        await self._stream.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._stream.__aexit__(exc_type, exc, tb)

    async def aclose(self) -> None:
        await self._stream.aclose()
