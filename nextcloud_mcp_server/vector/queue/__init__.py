"""Ingest-path ports & adapters (design §10, hexagonal)."""

from .factory import build_external_producer
from .memory import MemoryTaskProducer
from .ports import TaskProducer

__all__ = ["MemoryTaskProducer", "TaskProducer", "build_external_producer"]
