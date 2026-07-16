"""Unit tests for the native PDF-library serialization locks (concurrency safety).

pypdfium2 / pymupdf are not thread-safe; these locks serialize their native calls
so per-tier ingest concurrency (>1) can't drive a library from two threads at
once. See ``document_processors/_native_locks.py``.
"""

from __future__ import annotations

import threading
import time

import pytest

import nextcloud_mcp_server.document_processors._native_locks as native_locks
from nextcloud_mcp_server.document_processors._native_locks import (
    PDFIUM_LOCK,
    PYMUPDF_LOCK,
    pdfium_serialized,
    pymupdf_serialized,
)

pytestmark = pytest.mark.unit


def _max_concurrency(cm_factory, n: int = 8) -> int:
    """Run ``n`` threads through the context manager; return the max seen in-flight."""
    state_lock = threading.Lock()
    current = 0
    max_seen = 0

    def worker() -> None:
        nonlocal current, max_seen
        with cm_factory():
            with state_lock:
                current += 1
                max_seen = max(max_seen, current)
            time.sleep(0.01)  # widen the critical-section window so a race would show
            with state_lock:
                current -= 1

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return max_seen


def test_pymupdf_serialized_admits_one_thread_at_a_time():
    assert _max_concurrency(pymupdf_serialized) == 1


def test_pdfium_serialized_admits_one_thread_at_a_time():
    assert _max_concurrency(pdfium_serialized) == 1


def test_pdfium_and_pymupdf_locks_are_independent():
    """Separate locks let a PDFium parse and a MuPDF bbox overlap across documents."""
    assert PDFIUM_LOCK is not PYMUPDF_LOCK
    # Holding the MuPDF lock must not block acquiring the PDFium one.
    with pymupdf_serialized():
        acquired = PDFIUM_LOCK.acquire(blocking=False)
        assert acquired is True
        PDFIUM_LOCK.release()


def test_wait_metric_recorded_per_library(mocker):
    spy = mocker.spy(native_locks, "record_pdf_native_lock_wait")
    with pymupdf_serialized():
        pass
    with pdfium_serialized():
        pass
    assert [c.args[0] for c in spy.call_args_list] == ["pymupdf", "pdfium"]
    assert all(c.args[1] >= 0 for c in spy.call_args_list)  # non-negative wait
