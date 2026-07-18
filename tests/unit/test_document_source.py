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
