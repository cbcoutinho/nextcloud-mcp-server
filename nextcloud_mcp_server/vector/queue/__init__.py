"""Ingest-path ports & adapters (design §10, hexagonal; Deck #183)."""

from .factory import build_producer
from .memory import MemoryTaskProducer
from .ports import TaskProducer

__all__ = ["MemoryTaskProducer", "TaskProducer", "build_producer"]
