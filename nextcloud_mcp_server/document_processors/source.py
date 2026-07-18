"""A file-backed handle for a document being ingested.

The ingest path used to carry documents as ``bytes`` end to end, so peak memory
scaled with document size at every step: the WebDAV body was buffered whole, the
same buffer stayed live through parse, embed and bbox extraction, and the
structured tier pickled another two copies of it across the ``to_process`` pipe.
A 1040 MB document therefore could not be parsed at all -- the isolated worker
died at startup, before pymupdf4llm ran.

A :class:`DocumentSource` is a *path* plus the metadata callers used to read off
the buffer (``size``, ``content_type``). Both PDF engines open a path natively
and read incrementally, so handing the path down replaces those copies with
demand-paged reads:

* pypdfium2 -- ``FPDF_LoadDocument`` (path) instead of ``FPDF_LoadMemDocument64``
  (bytes), which pins the buffer for the document's lifetime.
* pymupdf -- ``fz_open_file`` instead of ``fz_open_memory``; PyMuPDF's own docs
  warn that the stream form may exhaust memory on large files.

Note that opening by path bounds the *input* copy, not the parse working set:
PDFium still retains parsed page objects, which is what page-windowed extraction
in ``pypdfium2_fast`` addresses. The two fixes are complementary.

``MemoryDocumentSource`` keeps the in-memory case first-class -- notes, deck
cards and small files never touch the disk, and every existing bytes-based test
keeps working -- while ``SpooledDocumentSource`` owns a temp file for the
lifetime of one ingest.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

SPOOL_PREFIX = "nc-ingest-"


@runtime_checkable
class DocumentSource(Protocol):
    """A document available to a processor, by path or in memory."""

    content_type: str
    filename: str | None

    @property
    def size(self) -> int:
        """Size of the document in bytes."""
        ...

    def path(self) -> Path:
        """A local filesystem path a native library can open.

        May block: for an in-memory source this writes the buffer to a temp
        file. Call :func:`resolve_path` from async code rather than calling this
        directly -- ingest workers run many documents on one event loop, so a
        synchronous multi-hundred-MB write would stall every other in-flight
        document for its duration.
        """
        ...

    def open(self) -> IO[bytes]:
        """A binary file object positioned at the start."""
        ...

    def read_bytes(self) -> bytes:
        """Materialise the whole document.

        The explicit escape hatch for consumers that genuinely need the bytes
        (OCR base64, the legacy ``process`` contract). Greppable on purpose: each
        call is a place where peak memory still scales with document size.
        """
        ...


@dataclass
class SpooledDocumentSource:
    """A document streamed to a local file, removed by :meth:`cleanup`."""

    spool_path: Path
    content_type: str
    filename: str | None = None
    _size: int | None = None

    @property
    def size(self) -> int:
        if self._size is None:
            self._size = self.spool_path.stat().st_size
        return self._size

    def path(self) -> Path:
        return self.spool_path

    def open(self) -> IO[bytes]:
        return self.spool_path.open("rb")

    def read_bytes(self) -> bytes:
        return self.spool_path.read_bytes()

    def cleanup(self) -> None:
        """Remove the spool file. Safe to call repeatedly."""
        try:
            self.spool_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort
            logger.warning("Could not remove spool file %s", self.spool_path)


@dataclass
class MemoryDocumentSource:
    """A document already in memory (notes, deck cards, small files, tests).

    ``path()`` materialises a temp file only if something actually asks for one,
    so the common small-document case never touches the disk.
    """

    content: bytes
    content_type: str
    filename: str | None = None
    _materialised: Path | None = field(default=None, repr=False)

    @property
    def size(self) -> int:
        return len(self.content)

    def path(self) -> Path:
        if self._materialised is None:
            fd, name = tempfile.mkstemp(prefix=SPOOL_PREFIX, suffix=".bin")
            with os.fdopen(fd, "wb") as fh:
                fh.write(self.content)
            self._materialised = Path(name)
        return self._materialised

    def open(self) -> IO[bytes]:
        import io  # noqa: PLC0415

        return io.BytesIO(self.content)

    def read_bytes(self) -> bytes:
        return self.content

    def cleanup(self) -> None:
        if self._materialised is not None:
            self._materialised.unlink(missing_ok=True)
            self._materialised = None


async def resolve_path(source: DocumentSource) -> Path:
    """Await a source's local path without blocking the event loop.

    ``SpooledDocumentSource.path()`` is already just an attribute read, but
    ``MemoryDocumentSource.path()`` writes the buffer to disk. Ingest workers run
    multiple documents concurrently on a single event loop, so that write is
    offloaded to a worker thread -- otherwise materialising one large document
    stalls every other job on the loop, which is exactly the case this ingest
    work exists to fix.
    """
    from anyio.to_thread import run_sync  # noqa: PLC0415 -- keep imports light

    return await run_sync(source.path)


@contextmanager
def spool_target(spool_dir: str | None = None) -> Iterator[Path]:
    """Yield a fresh spool path, removing it (and any partial file) on exit."""
    directory = spool_dir or tempfile.gettempdir()
    fd, name = tempfile.mkstemp(prefix=SPOOL_PREFIX, suffix=".bin", dir=directory)
    os.close(fd)
    target = Path(name)
    try:
        yield target
    finally:
        target.unlink(missing_ok=True)


def sweep_orphaned_spools(spool_dir: str | None = None) -> int:
    """Delete spool files left behind by a previous run; returns the count.

    Intended for a worker's startup path: a SIGKILLed worker cannot run its own
    cleanup, and the spool directory is an emptyDir that survives container
    restarts within the pod, so a crash-looping worker would otherwise accumulate
    whole documents on disk. NOT yet called anywhere -- it lands with the change
    that wires streaming downloads into the ingest path.
    """
    directory = Path(spool_dir or tempfile.gettempdir())
    removed = 0
    try:
        candidates = list(directory.glob(f"{SPOOL_PREFIX}*"))
    except OSError:  # pragma: no cover - unreadable spool dir
        return 0
    for stale in candidates:
        try:
            stale.unlink()
            removed += 1
        except OSError:  # pragma: no cover - raced with another sweeper
            continue
    if removed:
        logger.info(
            "Removed %d orphaned ingest spool file(s) from %s", removed, directory
        )
    return removed
