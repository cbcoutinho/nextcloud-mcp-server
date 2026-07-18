"""Spooled document downloads for the ingest path.

``read_file`` buffers a whole WebDAV response, so peak memory scaled with
document size: a 531 MB PDF OOMKilled a fast-tier worker mid-download, before
its size was ever evaluated. Streaming the body to a local file instead keeps
resident memory at one chunk regardless of how large the document is.

Lives in ``vector`` rather than ``document_processors`` because it needs the
Nextcloud client, and ``document_processors`` must not import ``vector`` (see
the layering note in ``document_processors/escalation.py``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from nextcloud_mcp_server.document_processors.source import (
    SpooledDocumentSource,
    spool_target,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def spooled_document(
    nc_client: Any,  # NextcloudClient; typed loosely to avoid a client import
    file_path: str,
    *,
    spool_dir: str | None = None,
    max_bytes: int | None = None,
) -> AsyncIterator[SpooledDocumentSource]:
    """Stream ``file_path`` to a spool file and yield it as a document source.

    The block owns the spool for its whole duration and removes it on exit,
    success or failure. Callers must therefore keep everything that touches the
    document -- parse, chunking, embedding, bbox extraction -- inside the block;
    nothing may hold the path afterwards.

    ``max_bytes`` aborts the transfer once the limit is exceeded. This is the
    guard that still holds when ``Content-Length`` is absent or untrue: the
    pre-flight size gate can only act on what the server advertised at scan
    time, whereas this acts on what actually arrives.
    """
    with spool_target(spool_dir) as target:
        written, content_type = await nc_client.webdav.stream_to_file(
            file_path, target, max_bytes=max_bytes
        )
        logger.debug(
            "Spooled %s to %s (%s bytes, %s)", file_path, target, written, content_type
        )
        yield SpooledDocumentSource(
            spool_path=target,
            content_type=content_type,
            filename=file_path,
            _size=written,
        )
