"""Unit tests for the spooled ingest download.

``spooled_document`` is what keeps peak memory off the document size: the body
is streamed to a file and the ingest path reads it by path. The properties worth
pinning are the ones that make that safe -- the spool is removed however the
block ends, and the yielded source describes the document that actually arrived.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.vector.spool import spooled_document

pytestmark = pytest.mark.unit


def _nc_client(
    body: bytes = b"%PDF-1.7 spooled", content_type: str = "application/pdf"
):
    """A client whose stream_to_file writes ``body`` to the destination."""
    nc = MagicMock()

    async def _stream_to_file(path, dest: Path, *, max_bytes=None):
        dest.write_bytes(body)
        return len(body), content_type

    nc.webdav.stream_to_file = AsyncMock(side_effect=_stream_to_file)
    return nc


async def test_yields_a_source_backed_by_the_spool_file(tmp_path):
    body = b"%PDF-1.7" + b"x" * 200
    nc = _nc_client(body)

    async with spooled_document(nc, "/f.pdf", spool_dir=str(tmp_path)) as source:
        assert source.path().exists()
        assert source.path().read_bytes() == body
        assert source.size == len(body)
        assert source.content_type == "application/pdf"
        assert source.filename == "/f.pdf"


async def test_spool_is_removed_on_success(tmp_path):
    nc = _nc_client()

    async with spooled_document(nc, "/f.pdf", spool_dir=str(tmp_path)) as source:
        spooled = source.path()
        assert spooled.exists()

    assert not spooled.exists(), "the block owns the spool and must clean it up"


async def test_spool_is_removed_when_the_body_raises(tmp_path):
    """A failure mid-processing must not leak a whole document onto disk."""
    nc = _nc_client()
    spooled: Path | None = None

    with pytest.raises(RuntimeError):
        async with spooled_document(nc, "/f.pdf", spool_dir=str(tmp_path)) as source:
            spooled = source.path()
            raise RuntimeError("parse blew up")

    assert spooled is not None
    assert not spooled.exists()


async def test_spool_is_removed_when_the_download_raises(tmp_path):
    nc = MagicMock()
    nc.webdav.stream_to_file = AsyncMock(side_effect=OSError("connection reset"))

    with pytest.raises(OSError):
        async with spooled_document(nc, "/f.pdf", spool_dir=str(tmp_path)):
            pass

    assert list(tmp_path.iterdir()) == [], "no spool file may survive a failed download"


async def test_max_bytes_is_forwarded_to_the_client(tmp_path):
    """The ceiling must reach the transport -- it is the guard that still holds
    when Content-Length is absent or wrong."""
    nc = _nc_client()

    async with spooled_document(nc, "/f.pdf", spool_dir=str(tmp_path), max_bytes=4096):
        pass

    assert nc.webdav.stream_to_file.await_args.kwargs["max_bytes"] == 4096


async def test_size_comes_from_bytes_written_not_a_stat(tmp_path):
    """The source reports what actually arrived."""
    body = b"x" * 321
    nc = _nc_client(body)

    async with spooled_document(nc, "/f.pdf", spool_dir=str(tmp_path)) as source:
        assert source.size == 321
