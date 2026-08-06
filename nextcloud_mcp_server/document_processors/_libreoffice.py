"""LibreOffice headless conversion, used to reach formats nothing else reads.

Legacy binary Office documents (``.doc``, ``.xls``) have no working pure-Python
reader: markitdown raises ``UnsupportedFormatException`` and docling-serve
returns ``status=failure``. LibreOffice is the only thing that opens them, and
it is already present in several of our images.

Two conversion targets, chosen per source format rather than uniformly:

* ``pdf`` -- for word-processor documents, whose page layout *is* their
  structure. The rendition is a real PDF, so the existing fast/structured
  ladder, ``page_boundaries`` and ``pdf_highlighter`` all work on it unchanged.
* ``xlsx`` -- for legacy spreadsheets, which are then read cell-by-cell. A
  spreadsheet must NOT go to PDF: measured on a real workbook, PDF rendering
  paginated it into 25 mixed-orientation pages and recalled only 63.8% of the
  tokens a direct cell read recovers, because LibreOffice honours the print
  layout and drops whatever falls outside it.
"""

import logging
import pathlib
import shutil
import tempfile
from typing import Final

import anyio

logger = logging.getLogger(__name__)

# Resolved once at import, like TesseractProcessor's availability probe, so a
# deployment without LibreOffice degrades to "no processor for this type"
# rather than failing per document.
SOFFICE_BIN: Final[str | None] = shutil.which("soffice") or shutil.which("libreoffice")
LIBREOFFICE_AVAILABLE: Final[bool] = SOFFICE_BIN is not None


class LibreOfficeError(Exception):
    """Raised when a LibreOffice conversion fails or produces no output."""


async def convert(
    content: bytes,
    filename: str,
    target: str,
    timeout: float = 120.0,
) -> bytes:
    """Convert ``content`` to ``target`` ("pdf" / "xlsx") and return the bytes.

    Args:
        content: Source document bytes.
        filename: Source filename -- LibreOffice picks its import filter from
            the extension, so a name without one converts as the wrong format.
        target: LibreOffice output filter name.
        timeout: Wall-clock cap; the process is killed past it.

    Raises:
        LibreOfficeError: LibreOffice is absent, exits non-zero, times out, or
            writes no output file.
    """
    if SOFFICE_BIN is None:
        raise LibreOfficeError("LibreOffice (soffice) is not installed")

    suffix = pathlib.Path(filename).suffix
    if not suffix:
        raise LibreOfficeError(
            f"cannot convert {filename!r}: no extension to select an import filter"
        )

    with tempfile.TemporaryDirectory(prefix="lo-convert-") as tmp:
        tmpdir = pathlib.Path(tmp)
        src = tmpdir / f"source{suffix}"
        src.write_bytes(content)
        outdir = tmpdir / "out"
        outdir.mkdir()

        # -env:UserInstallation gives this invocation a private profile
        # directory. Without it every concurrent soffice shares one profile and
        # the second one either blocks on the lock or exits 0 having written
        # nothing -- which would surface as a random empty-output failure under
        # parallel ingest rather than as anything diagnosable.
        profile = tmpdir / "profile"
        argv = [
            SOFFICE_BIN,
            f"-env:UserInstallation=file://{profile}",
            "--headless",
            "--norestore",
            "--convert-to",
            target,
            "--outdir",
            str(outdir),
            str(src),
        ]

        try:
            with anyio.fail_after(timeout):
                result = await anyio.run_process(argv, check=False)
        except TimeoutError as exc:
            raise LibreOfficeError(
                f"LibreOffice timed out after {timeout}s converting {filename!r}"
            ) from exc

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()[:500]
            raise LibreOfficeError(
                f"LibreOffice exited {result.returncode} for {filename!r}: {stderr}"
            )

        # soffice exits 0 on an unreadable input while writing nothing, so the
        # output file's existence -- not the return code -- is the real check.
        produced = sorted(outdir.iterdir())
        if not produced:
            raise LibreOfficeError(
                f"LibreOffice produced no {target} output for {filename!r}"
            )

        data = produced[0].read_bytes()
        logger.debug(
            "LibreOffice converted %s (%d bytes) -> %s (%d bytes)",
            filename,
            len(content),
            target,
            len(data),
        )
        return data
