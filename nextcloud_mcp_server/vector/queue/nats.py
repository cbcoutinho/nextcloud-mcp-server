"""NATS JetStream ``TaskProducer`` — external ingest transport (design §3.4).

Publishes ``mcp.ingest.requested.{tenant_id}`` for the external
document-processor to consume. Translates the in-process ``DocumentTask`` into
the wire ``IngestMessage`` schema (mirrored in astrolabe-cloud-website's
``bus/messages.py``), with the JetStream ``Nats-Msg-Id`` dedup header per §3.4.

This server is only the *producer* on this transport; the document-processor
owns the consumer. ``nats-py`` is imported lazily so deployments that never
enable external ingest don't pay the import.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from types import TracebackType
from typing import TYPE_CHECKING, Any

from ...canonical import canonical_json

if TYPE_CHECKING:
    from ..scanner import DocumentTask

logger = logging.getLogger(__name__)

STREAM_NAME = "mcp"
INGEST_SUBJECT_PREFIX = "mcp.ingest.requested"


def warn_if_insecure_nats_url(url: str) -> None:
    """Log a warning when the bus URL is not TLS-encrypted.

    ``nats://`` (and ``ws://``) carry tenant document metadata in cleartext;
    production deployments should use ``tls://`` (or ``wss://``). We connect
    regardless — this is an operator alert, not a hard failure.
    """
    scheme = url.split("://", 1)[0].lower()
    if scheme not in ("tls", "wss"):
        logger.warning(
            "NATS bus URL uses unencrypted transport (scheme=%s://); "
            "use tls:// in production to protect document metadata in transit",
            scheme,
        )


def _modified_at_rfc3339(modified_at: int) -> str:
    """DocumentTask.modified_at is an epoch int (0 for deletes)."""
    return datetime.fromtimestamp(int(modified_at), tz=timezone.utc).isoformat()


def _content_hash(task: DocumentTask) -> str:
    """etag is the change-detection token; fall back to modified_at when it is
    absent (e.g. deletes, or sources whose etag we don't thread through).

    TODO(follow-up, PR #814 review): thread etags for file / deck_card /
    news_item scans too (only note scans pass etag today). Until then their
    JetStream Nats-Msg-Id dedup keys off modified_at, which misses content
    changes that leave modified_at unchanged (e.g. a file move/rename).
    """
    return task.etag or str(task.modified_at)


def msg_id(tenant_id: str, doc_id: str, modified_at_rfc3339: str) -> str:
    """JetStream dedup header per §3.4. SHA-256 over canonical JSON (NOT the
    BLAKE2b helper) — it is an opaque external header, not a stored field."""
    return hashlib.sha256(
        canonical_json(
            {
                "tenant_id": tenant_id,
                "doc_id": doc_id,
                "modified_at": modified_at_rfc3339,
            }
        )
    ).hexdigest()


class NatsTaskProducer:
    """Publishes ingest requests to NATS JetStream."""

    def __init__(self, nc: Any, js: Any, tenant_id: str):
        self._nc = nc
        self._js = js
        self.tenant_id = tenant_id

    @classmethod
    async def connect(
        cls, *, url: str, tenant_id: str, num_replicas: int = 1
    ) -> NatsTaskProducer:
        import nats  # noqa: PLC0415  (lazy: optional dependency for external mode)

        warn_if_insecure_nats_url(url)
        nc = await nats.connect(url)
        js = nc.jetstream()
        await cls._ensure_stream(js, num_replicas)
        logger.info("Connected NATS ingest producer: url=%s, tenant=%s", url, tenant_id)
        return cls(nc, js, tenant_id)

    @staticmethod
    async def _ensure_stream(js: Any, num_replicas: int) -> None:
        # noqa: PLC0415 — nats.js types are only importable once nats-py is present.
        from nats.js.api import RetentionPolicy, StreamConfig  # noqa: PLC0415

        config = StreamConfig(
            name=STREAM_NAME,
            subjects=["mcp.>"],
            retention=RetentionPolicy.LIMITS,
            num_replicas=num_replicas,
        )
        try:
            await js.add_stream(config=config)
            logger.info("nats.stream_created stream=%s", STREAM_NAME)
        except Exception as exc:
            # add_stream is idempotent in spirit but errors when the stream
            # already exists; treat as benign (mirrors the processor's
            # ensure_stream). A genuinely broken broker surfaces on publish.
            logger.info("nats.stream_exists_or_unavailable detail=%s", exc)

    def ingest_message(self, task: DocumentTask) -> dict[str, Any]:
        """DocumentTask → wire IngestMessage dict (mirrors the sibling schema)."""
        return {
            "tenant_id": self.tenant_id,
            "doc_id": task.doc_id,
            "content_hash": _content_hash(task),
            "modified_at": _modified_at_rfc3339(task.modified_at),
            "doc_type": task.doc_type,
            "operation": task.operation,
            "user_id": task.user_id,
            "file_path": task.file_path,
        }

    async def send(self, task: DocumentTask) -> None:
        message = self.ingest_message(task)
        subject = f"{INGEST_SUBJECT_PREFIX}.{self.tenant_id}"
        headers = {
            "Nats-Msg-Id": msg_id(self.tenant_id, task.doc_id, message["modified_at"])
        }
        await self._js.publish(subject, canonical_json(message), headers=headers)

    # The scanner/oauth_sync use the producer as a clone-able async context
    # manager (memory-stream semantics). The bus connection is owned by the
    # lifespan, so cloning shares it and __aexit__ is a no-op (close happens via
    # aclose() on shutdown).
    def clone(self) -> NatsTaskProducer:
        return self

    async def __aenter__(self) -> NatsTaskProducer:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    # The bare suppression marker silences S7503 (async method without await):
    # ``async def`` is required by the TaskProducer protocol, but this handle
    # close is a genuine no-op.
    async def aclose(self) -> None:  # NOSONAR
        # Per-handle close (e.g. a per-user scanner clone exiting). The bus
        # connection is shared and owned by the lifespan, so this is a no-op;
        # the connection is torn down once via ``drain()`` on shutdown.
        return None

    async def drain(self) -> None:
        """Drain + close the shared NATS connection (lifespan shutdown only)."""
        try:
            await self._nc.drain()
        except Exception:
            logger.warning("NATS drain on shutdown failed", exc_info=True)
