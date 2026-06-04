"""Subprocess isolation for PDF parsing.

PDF text/markdown extraction (pymupdf + pymupdf4llm) is CPU-bound C code that can
balloon memory or hang on a pathological document -- e.g. a page with ~1M vector
path items drives pymupdf4llm's table detection past the pod memory limit and
OOM-kills the whole process. Running the parse in a worker *subprocess* (via
``anyio.to_process``) with an address-space rlimit and a wall-clock timeout means
one bad file fails *that document*, not the pod: an rlimit breach raises
``MemoryError`` in the worker, a hang is killed when the timeout cancels the call.

The worker function is module-level (picklable) so ``anyio.to_process`` can run it
in its process pool.
"""

import logging
import resource
from pathlib import Path
from typing import Any

import anyio
import anyio.to_process
from anyio import BrokenWorkerProcess

logger = logging.getLogger(__name__)

# Guard so the address-space limit is applied once per (reused) worker process.
_MEM_LIMIT_APPLIED = False


class PdfParseFailed(Exception):
    """A PDF parse failed in the isolated worker.

    ``reason`` is one of ``timeout`` | ``oom`` | ``error`` and maps directly to
    the ``astrolabe_document_parse_failed_total{reason}`` metric label.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or f"PDF parse failed ({reason})")


def _apply_mem_limit(mem_limit_mb: int) -> None:
    """Cap the worker's address space (RLIMIT_AS) so a bomb raises MemoryError.

    Applied once per worker process. The hard limit is left untouched (we only
    lower the soft limit), and we never set a soft limit above the hard limit.
    """
    global _MEM_LIMIT_APPLIED
    if _MEM_LIMIT_APPLIED or mem_limit_mb <= 0:
        return
    target = mem_limit_mb * 1024 * 1024
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    soft_target = target if hard == resource.RLIM_INFINITY else min(target, hard)
    resource.setrlimit(resource.RLIMIT_AS, (soft_target, hard))
    _MEM_LIMIT_APPLIED = True


def _parse_pdf_worker(
    content: bytes,
    write_images: bool,
    image_path: str | None,
    graphics_limit: int,
    mem_limit_mb: int,
) -> list[dict[str, Any]]:
    """Run pymupdf4llm.to_markdown in the worker subprocess (positional args only).

    Returns the ``page_chunks`` list (picklable dicts of text + metadata). Imports
    pymupdf lazily so the parent process isn't forced to load them here.
    """
    _apply_mem_limit(mem_limit_mb)

    # Imported inside the worker so the parent process (and any module that
    # imports this one) doesn't load pymupdf4llm -- which prints a banner to
    # stdout on import, the channel anyio's worker uses for IPC.
    import pymupdf  # noqa: PLC0415
    import pymupdf4llm  # noqa: PLC0415

    doc = pymupdf.open("pdf", content)
    try:
        # page_chunks=True makes to_markdown return list[dict], not str.
        return pymupdf4llm.to_markdown(  # type: ignore[return-value]
            doc,
            write_images=write_images,
            image_path=image_path if write_images else None,
            page_chunks=True,
            graphics_limit=graphics_limit,
        )
    finally:
        doc.close()


async def run_isolated_pdf_parse(
    content: bytes,
    *,
    write_images: bool,
    image_path: Path | None,
    graphics_limit: int,
    timeout_seconds: float,
    mem_limit_mb: int,
) -> list[dict[str, Any]]:
    """Parse a PDF in an isolated worker subprocess with a memory cap and timeout.

    Raises ``PdfParseFailed`` (reason ``timeout`` | ``oom`` | ``error``) instead of
    taking the pod down. On timeout the worker process is killed (``cancellable``).
    """
    with anyio.move_on_after(timeout_seconds):
        try:
            return await anyio.to_process.run_sync(
                _parse_pdf_worker,
                content,
                write_images,
                str(image_path) if image_path is not None else None,
                graphics_limit,
                mem_limit_mb,
                cancellable=True,
            )
        except MemoryError as e:
            raise PdfParseFailed("oom", str(e)) from e
        except BrokenWorkerProcess as e:
            # Worker died without a clean exception (e.g. SIGKILL from the OS OOM
            # killer beating the rlimit). Treat as an out-of-memory failure.
            raise PdfParseFailed("oom", str(e)) from e
        except Exception as e:
            raise PdfParseFailed("error", f"{type(e).__name__}: {e}") from e
    # Reached only when move_on_after swallowed the timeout cancellation.
    raise PdfParseFailed("timeout", f"parse exceeded {timeout_seconds}s")
