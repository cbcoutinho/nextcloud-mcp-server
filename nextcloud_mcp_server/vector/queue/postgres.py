"""Postgres-queue ``TaskProducer`` — documented seam, not implemented.

The external document-processor may later drain a Postgres-backed queue instead
of NATS to limit NATS operational overhead. Processing stays *external*; only
the transport changes — so on this server it would be a drop-in producer swap.
The consume side + the queue-table migration belong to that processor-side
refactor (cross-repo), NOT here. This stub exists so the transport value and the
``TaskProducer`` Protocol conformance are testable today.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..scanner import DocumentTask

_NOT_IMPLEMENTED = (
    "Postgres ingest transport is a documented seam. The external "
    "document-processor owns the Postgres-drain refactor (transport swap only; "
    "processing stays external). Use INGEST_BUS_URL=nats://… for now."
)


class PostgresTaskProducer:
    @classmethod
    async def connect(cls, settings: Any) -> PostgresTaskProducer:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def send(self, task: DocumentTask) -> None:  # pragma: no cover
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def clone(self) -> PostgresTaskProducer:  # pragma: no cover
        return self

    async def __aenter__(self) -> PostgresTaskProducer:  # pragma: no cover
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:  # pragma: no cover
        return None

    # The bare suppression marker silences S7503 (async method without await):
    # ``async def`` is required by the TaskProducer protocol; this stub is a
    # no-op until the Postgres transport lands.
    async def aclose(self) -> None:  # NOSONAR  # pragma: no cover
        return None
