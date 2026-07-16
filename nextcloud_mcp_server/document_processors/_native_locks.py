"""Process-global serialization for the non-thread-safe native PDF libraries.

pypdfium2 (PDFium) and pymupdf (MuPDF) each keep process-global state — a single
library instance per process; pypdfium2 additionally mutates an *unlocked*
module-global object tracker on every object open/close. The ingest worker
offloads their CPU-bound work to the shared anyio thread pool, and once a tier
runs with ``concurrency > 1`` (or the ``ocr`` tier's existing ``concurrency:
24``), multiple jobs can drive the same native library from different threads at
once — a data race that segfaults or corrupts output.

These module-level ``threading.Lock``\\s serialize each library's native calls.
Two *separate* locks (the libraries are independent) let a PDFium parse and a
MuPDF bbox overlap across concurrent documents while forbidding same-library
concurrency. A ``threading.Lock`` (not an anyio ``CapacityLimiter``) is used
deliberately: some MuPDF work runs on the event loop (the escalation classifier's
``image_coverage_per_page``), not only in worker threads, so the primitive must
be acquirable from both — an async limiter cannot be awaited from sync-on-loop
code.

Observability: the wait to acquire each lock is recorded to
``astrolabe_pdf_native_lock_wait_seconds{library=...}`` so lock contention under
concurrency is visible in Prometheus. The offload's *total* time already appears
in the enclosing OTel span (e.g. ``vector_sync.compute_chunk_bboxes``,
``document_processor.parse``), and each document keeps its own trace id, so
concurrent jobs stay attributable in traces/logs.
"""

import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

from nextcloud_mcp_server.observability.metrics import record_pdf_native_lock_wait

# Independent process-global native libraries → independent locks, so a PDFium
# parse and a MuPDF bbox may overlap across concurrent documents.
PDFIUM_LOCK = threading.Lock()
PYMUPDF_LOCK = threading.Lock()


@contextmanager
def _serialized(lock: threading.Lock, library: str) -> Iterator[None]:
    start = time.perf_counter()
    lock.acquire()
    record_pdf_native_lock_wait(library, time.perf_counter() - start)
    try:
        yield
    finally:
        lock.release()


def pdfium_serialized() -> AbstractContextManager[None]:
    """Serialize a pypdfium2 (PDFium) native section across threads."""
    return _serialized(PDFIUM_LOCK, "pdfium")


def pymupdf_serialized() -> AbstractContextManager[None]:
    """Serialize a pymupdf (MuPDF) native section across threads and the loop."""
    return _serialized(PYMUPDF_LOCK, "pymupdf")
