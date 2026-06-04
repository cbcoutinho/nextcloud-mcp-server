"""Ingest-path ports & adapters (design §10, hexagonal; Deck #183)."""

from .factory import build_producer
from .memory import MemoryTaskProducer
from .ports import TaskProducer
from .transport import (
    DistributedTransport,
    IngestTransport,
    LocalTransport,
    SpawnWorker,
    build_transport,
)

__all__ = [
    "DistributedTransport",
    "IngestTransport",
    "LocalTransport",
    "MemoryTaskProducer",
    "SpawnWorker",
    "TaskProducer",
    "build_producer",
    "build_transport",
]
