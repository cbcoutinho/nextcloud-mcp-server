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
import sys
from pathlib import Path
from typing import Any, cast

import anyio
import anyio.to_process
from anyio import BrokenWorkerProcess, CapacityLimiter
from anyio.lowlevel import RunVar

# ``resource`` is a Unix-only stdlib module -- it does not exist on Windows, and
# importing it unconditionally crashed Windows startup (#877). The RLIMIT_AS cap
# it provides is a Linux-pod safety measure, not a correctness requirement, so on
# platforms without it we fall back to a no-op (``resource is None``).
if sys.platform == "win32":  # pragma: no cover - win32-only path
    resource = None
else:
    import resource

logger = logging.getLogger(__name__)

# Guard so the address-space limit is applied once per (reused) worker process.
_MEM_LIMIT_APPLIED = False

# Bounds how many parse subprocesses may run at once. anyio's default process
# limiter is os.cpu_count(), which is decoupled from both the worker's
# --concurrency and the pod memory limit: on an 8-core node that allows 8 x
# document_parse_mem_limit_mb of address space inside a 3 GiB pod. A CapacityLimiter
# belongs to the event loop that created it, so hold it in a RunVar (the same
# mechanism anyio uses for its own default) rather than a module global.
_PARSE_LIMITER: RunVar[CapacityLimiter] = RunVar("_pdf_parse_process_limiter")


def parse_process_limiter(slots: int) -> CapacityLimiter:
    """The per-event-loop limiter bounding concurrent parse subprocesses.

    Created on first use from ``document_parse_process_slots``. Later changes to
    the setting do not resize an existing limiter -- like the RLIMIT_AS cap it
    pairs with, it is fixed for the worker's lifetime and a change needs a
    restart.
    """
    try:
        return _PARSE_LIMITER.get()
    except LookupError:
        limiter = CapacityLimiter(max(1, slots))
        _PARSE_LIMITER.set(limiter)
        return limiter


# pymupdf4llm reconstructs reading-order markdown, which can DROP most of the text
# on non-prose layouts (engineering drawings, scattered labels): observed 598 of a
# 5080-char text layer on an A1 ventilation drawing -- which then mis-escalated to
# the (paid) OCR tier despite the layer already containing the answer. When a page's
# markdown recovers far less than its raw text layer, fall back to plain get_text for
# that page: the text layer is the source of truth; markdown's value is structure,
# not completeness, and the structured/pymupdf tier exists precisely to re-extract a
# layer correctly. Trigger only on a CLEAR under-extraction (raw >= ratio x markdown
# AND raw non-trivial) so clean prose -- whose markdown matches/exceeds get_text -- is
# untouched.
_TEXTLAYER_FALLBACK_RATIO = 3.0
_TEXTLAYER_FALLBACK_MIN_CHARS = 64


def _recover_underextracted_pages(doc: Any, chunks: list[dict[str, Any]]) -> None:
    """Replace a page's markdown with its raw ``get_text`` layer (in place) when
    ``to_markdown`` dropped most of it -- see ``_TEXTLAYER_FALLBACK_*``.

    page_chunks=True yields one chunk per page in page order, so ``chunks[i]`` maps
    to ``doc[i]`` (this worker always parses the full doc -- no ``pages=`` subset).
    Per-page best effort: a get_text failure leaves that page's markdown as-is.
    """
    for i, chunk in enumerate(chunks):
        if i >= doc.page_count:
            break
        md = (chunk.get("text") or "").strip()
        try:
            raw = doc[i].get_text("text")
        except Exception:  # noqa: BLE001 -- per-page best effort, never fail the parse
            continue
        raw_len = len(raw.strip())
        if raw_len >= _TEXTLAYER_FALLBACK_MIN_CHARS and raw_len >= (
            _TEXTLAYER_FALLBACK_RATIO * max(len(md), 1)
        ):
            chunk["text"] = raw


def uses_markdown(page_count: int, markdown_max_pages: int) -> bool:
    """Whether a document of ``page_count`` pages gets markdown reconstruction.

    Single source of truth for the page gate (Deck #399). The worker uses it to
    choose the parse path; ``PyMuPDFProcessor`` uses it to label
    ``astrolabe_document_parse_mode_total``. Duplicating the comparison would let
    the metric drift from the decision it claims to describe.

    ``markdown_max_pages <= 0`` disables markdown entirely; the bound is
    inclusive, so a document exactly at the ceiling still gets markdown.
    """
    return markdown_max_pages > 0 and page_count <= markdown_max_pages


def _text_only_chunks(doc: Any) -> list[dict[str, Any]]:
    """Build ``page_chunks``-shaped output from the raw text layer alone.

    Emits the same contract ``to_markdown(page_chunks=True)`` does, as far as
    callers actually consume it: ``PyMuPDFProcessor._build_page_boundaries``
    reads only ``chunk["text"]`` and ``chunk["metadata"]["page_number"]`` (with
    a positional fallback for the latter), so page boundaries, the chunker and
    bbox computation are unaffected by which path produced the list.

    ``metadata.page_number`` is 1-based, matching the key pymupdf4llm's
    layout path emits (``document_layout``); the pin in ``pyproject.toml`` is
    what makes that key a fixed target rather than a guess.
    """
    chunks: list[dict[str, Any]] = []
    for i in range(doc.page_count):
        try:
            text = doc[i].get_text("text")
        # Per-page best effort, mirroring the markdown path above: one
        # unreadable page degrades to "" rather than failing the document.
        except Exception:  # noqa: BLE001
            text = ""
        chunks.append({"text": text, "metadata": {"page_number": i + 1}})
    return chunks


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

    On a platform without the Unix-only ``resource`` module (e.g. Windows, see
    #877) the cap is skipped -- the worker still runs, just without the
    address-space limit.
    """
    global _MEM_LIMIT_APPLIED
    if _MEM_LIMIT_APPLIED or mem_limit_mb <= 0:
        return
    if resource is None:
        logger.debug("resource module unavailable; skipping RLIMIT_AS cap")
        _MEM_LIMIT_APPLIED = True
        return
    target = mem_limit_mb * 1024 * 1024
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    soft_target = target if hard == resource.RLIM_INFINITY else min(target, hard)
    resource.setrlimit(resource.RLIMIT_AS, (soft_target, hard))
    _MEM_LIMIT_APPLIED = True


def _parse_pdf_worker(
    source_path: str,
    write_images: bool,
    image_path: str | None,
    graphics_limit: int,
    mem_limit_mb: int,
    markdown_max_pages: int,
) -> list[dict[str, Any]]:
    """Extract a PDF in the worker subprocess (positional args only).

    Runs ``pymupdf4llm.to_markdown`` when :func:`uses_markdown` allows it for this
    document's page count, and falls back to the raw text layer
    (:func:`_text_only_chunks`) otherwise. Both paths return the same
    ``page_chunks`` shape, so the caller does not branch.

    Takes a PATH, not bytes. Passing bytes meant every argument crossing the
    ``to_process`` pipe was a full copy of the document -- one in the parent, one
    pickled into the pipe, one in the child -- so a large file breached the
    child's RLIMIT_AS before pymupdf4llm ran at all. Measured on a real 1040 MB
    document: the worker died during initialisation in 0.9s while the parent
    peaked at 2185 MB. Opening the path uses ``fz_open_file``, which reads
    incrementally, so the rlimit now bounds parse working set as intended.

    Returns the ``page_chunks`` list (picklable dicts of text + metadata). Imports
    pymupdf lazily so the parent process isn't forced to load them here.
    """
    _apply_mem_limit(mem_limit_mb)

    # Imported inside the worker so the parent process (and any module that
    # imports this one) doesn't load pymupdf4llm -- which prints a banner to
    # stdout on import, the channel anyio's worker uses for IPC.
    import pymupdf  # noqa: PLC0415
    import pymupdf4llm  # noqa: PLC0415

    doc = pymupdf.open(source_path)
    try:
        # Page gate (Deck #399). to_markdown is superlinear in page count, so a
        # large document burns the entire parse timeout and then dead-letters
        # reason="timeout" -- discarding a text layer that get_text extracts in
        # ~4.5 ms/page. Decide from page_count BEFORE parsing: a runtime budget
        # would still pay the full timeout on every doomed document to learn
        # what page_count gives for free. page_count is metadata, so this costs
        # nothing beyond the open we already did.
        if not uses_markdown(doc.page_count, markdown_max_pages):
            return _text_only_chunks(doc)

        # page_chunks=True makes to_markdown return list[dict], not str.
        chunks = cast(
            "list[dict[str, Any]]",
            pymupdf4llm.to_markdown(
                doc,
                write_images=write_images,
                image_path=image_path if write_images else None,
                page_chunks=True,
                graphics_limit=graphics_limit,
            ),
        )
        # Recover pages where markdown reconstruction dropped most of the text layer.
        _recover_underextracted_pages(doc, chunks)
        return chunks
    finally:
        doc.close()


async def run_isolated_pdf_parse(
    source_path: str,
    *,
    write_images: bool,
    image_path: Path | None,
    graphics_limit: int,
    timeout_seconds: float,
    mem_limit_mb: int,
    markdown_max_pages: int,
    process_slots: int = 2,
) -> list[dict[str, Any]]:
    """Parse a PDF in an isolated worker subprocess with a memory cap and timeout.

    Raises ``PdfParseFailed`` (reason ``timeout`` | ``oom`` | ``error``) instead of
    taking the pod down. On timeout the worker process is killed (``cancellable``).

    ``process_slots`` bounds how many parses run concurrently. Without it anyio
    defaults to an ``os.cpu_count()``-wide pool, which neither the worker's
    ``--concurrency`` nor the pod memory limit constrains.

    ``markdown_max_pages`` is required rather than defaulted: <=0 legitimately
    means "never run to_markdown", so a default would silently pick a parse mode
    for any caller that forgot to pass one.
    """
    with anyio.move_on_after(timeout_seconds):
        try:
            return await anyio.to_process.run_sync(
                _parse_pdf_worker,
                source_path,
                write_images,
                str(image_path) if image_path is not None else None,
                graphics_limit,
                mem_limit_mb,
                markdown_max_pages,
                cancellable=True,
                limiter=parse_process_limiter(process_slots),
            )
        except MemoryError as e:
            # A clean rlimit breach: the worker raised MemoryError and stays
            # ALIVE in anyio's pool (unlike the BrokenWorkerProcess/SIGKILL path,
            # which spawns a fresh worker). Its heap may be slightly fragmented
            # for the next document. Acceptable: RLIMIT_AS caps virtual address
            # space (not RSS), so practical fragmentation risk is low.
            raise PdfParseFailed("oom", str(e)) from e
        except BrokenWorkerProcess as e:
            # Worker died without a clean exception (e.g. SIGKILL from the OS OOM
            # killer beating the rlimit). Treat as an out-of-memory failure.
            raise PdfParseFailed("oom", str(e)) from e
        except Exception as e:
            raise PdfParseFailed("error", f"{type(e).__name__}: {e}") from e
    # Reached only when move_on_after swallowed the timeout cancellation.
    raise PdfParseFailed("timeout", f"parse exceeded {timeout_seconds}s")
