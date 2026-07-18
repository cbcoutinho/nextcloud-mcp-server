"""Unit tests for the file-backed document handle.

``DocumentSource`` exists so a processor can open a document by path instead of
receiving its bytes. The properties worth pinning are the ones that keep peak
memory bounded (a spooled source never materialises), the ones that keep the
in-memory case cheap (a small document never touches disk unless asked), and the
cleanup behaviour a crash-looping worker depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nextcloud_mcp_server.document_processors.source import (
    SPOOL_PREFIX,
    MemoryDocumentSource,
    SpooledDocumentSource,
    spool_target,
    sweep_orphaned_spools,
)

pytestmark = pytest.mark.unit


def _spooled(tmp_path: Path, body: bytes = b"%PDF-1.7 body") -> SpooledDocumentSource:
    target = tmp_path / "doc.bin"
    target.write_bytes(body)
    return SpooledDocumentSource(target, "application/pdf", "doc.pdf")


def test_spooled_source_exposes_path_size_and_bytes(tmp_path):
    body = b"%PDF-1.7" + b"x" * 100
    source = _spooled(tmp_path, body)

    assert source.path().exists()
    assert source.size == len(body)
    assert source.read_bytes() == body
    with source.open() as fh:
        assert fh.read() == body


def test_spooled_size_does_not_read_the_file(tmp_path):
    """size must come from stat, not from materialising the document."""
    source = _spooled(tmp_path, b"x" * 4096)

    # Truncating after the first stat would change the answer if size re-read.
    assert source.size == 4096
    source.path().write_bytes(b"")
    assert source.size == 4096, "size should be cached from the initial stat"


def test_spooled_cleanup_is_idempotent(tmp_path):
    source = _spooled(tmp_path)

    source.cleanup()
    source.cleanup()  # must not raise on an already-removed file

    assert not source.path().exists()


def test_memory_source_does_not_touch_disk_until_a_path_is_asked_for(tmp_path):
    source = MemoryDocumentSource(b"hello", "text/plain", "n.txt")

    assert source.size == 5
    assert source.read_bytes() == b"hello"
    assert source._materialised is None, "no path requested -> no temp file"

    path = source.path()
    try:
        assert path.exists() and path.read_bytes() == b"hello"
    finally:
        source.cleanup()
    assert not path.exists()


def test_memory_source_path_is_stable_across_calls():
    source = MemoryDocumentSource(b"hello", "text/plain")
    try:
        assert source.path() == source.path()
    finally:
        source.cleanup()


def test_spool_target_removes_the_file_even_on_failure(tmp_path):
    manager = spool_target(str(tmp_path))
    captured = manager.__enter__()
    captured.write_bytes(b"partial download")
    assert captured.exists()

    # Simulate a download blowing up part-way through.
    manager.__exit__(RuntimeError, RuntimeError("download blew up"), None)

    assert not captured.exists(), "a partial download must not be left behind"


def test_sweep_removes_orphans_but_leaves_other_files(tmp_path):
    """A SIGKILLed worker cannot clean up, and the spool dir survives restarts."""
    orphan = tmp_path / f"{SPOOL_PREFIX}abc.bin"
    orphan.write_bytes(b"leaked document")
    unrelated = tmp_path / "keep-me.txt"
    unrelated.write_bytes(b"not ours")

    removed = sweep_orphaned_spools(str(tmp_path))

    assert removed == 1
    assert not orphan.exists()
    assert unrelated.exists(), "the sweep must only claim files it created"


def test_sweep_on_empty_directory_is_a_noop(tmp_path):
    assert sweep_orphaned_spools(str(tmp_path)) == 0


def test_is_file_backed_distinguishes_the_two_sources(tmp_path):
    """Sync callers use this to pick the access that does no I/O.

    A spooled source hands over its path for free; an in-memory one hands over
    its buffer for free. Choosing wrong means a blocking whole-buffer disk write
    on the shared event loop (registry._classify_result is a plain def).
    """
    spooled = _spooled(tmp_path)
    memory = MemoryDocumentSource(b"hello", "text/plain")

    assert spooled.is_file_backed is True
    assert memory.is_file_backed is False


def test_memory_source_free_access_does_not_touch_disk():
    """read_bytes on an in-memory source must not materialise anything."""
    source = MemoryDocumentSource(b"hello", "text/plain")

    assert source.read_bytes() == b"hello"
    assert source._materialised is None


def test_memory_source_materialises_into_the_configured_spool_dir(tmp_path):
    """Both source types must land in the directory the worker actually sweeps.

    The startup sweep only globs ``document_spool_dir``. If an in-memory source
    materialised to the system temp dir instead, a SIGKILL on the buffered path
    would leave an orphan nothing ever collects -- and that disk usage would sit
    outside the volume the spool budget sizes.
    """
    source = MemoryDocumentSource(b"hello", "text/plain", spool_dir=str(tmp_path))
    try:
        path = source.path()
        assert path.parent == tmp_path
        assert path.name.startswith(SPOOL_PREFIX)
        # ...and therefore the sweep can actually find it.
        assert sweep_orphaned_spools(str(tmp_path)) == 1
    finally:
        source.cleanup()


def test_memory_source_defaults_to_the_system_temp_dir():
    import tempfile as _tempfile

    source = MemoryDocumentSource(b"hello", "text/plain")
    try:
        assert source.path().parent == Path(_tempfile.gettempdir())
    finally:
        source.cleanup()
