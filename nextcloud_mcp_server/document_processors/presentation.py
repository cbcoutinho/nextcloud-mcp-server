"""Presentations, read shape-by-shape rather than through a PDF rendition.

``.pptx`` is already OOXML (a zip of XML parts), so ``python-pptx`` reads it
directly -- no LibreOffice/``soffice`` dependency. Legacy ``.ppt`` (OLE2) is
out of scope: python-pptx cannot open the OLE2 container, only the OOXML one.
"""

import io
import logging
import zipfile
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from anyio.to_thread import run_sync

from .base import DocumentProcessor, ProcessingResult, ProcessorError

logger = logging.getLogger(__name__)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Per-slide spans into the joined text: {"slide", "start_offset", "end_offset"}.
# The presentation counterpart to a PDF's ``page_boundaries`` -- lets a chunk be
# attributed to the slide it came from.
SLIDE_BOUNDARIES_KEY = "slide_boundaries"

# python-pptx inflates every package part into memory, so bound the total
# *declared* uncompressed size before opening it (zipfile will not inflate past
# a member's declared size, so the header cannot lie its way around this). The
# download ceiling only caps the compressed bytes; this stops a zip bomb.
# ponytail: fixed cap, make it a setting if real decks hit it.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024


class PptxProcessor(DocumentProcessor):
    """Extract ``.pptx`` as one markdown section per slide."""

    @property
    def name(self) -> str:
        return "presentation"

    @property
    def tier(self) -> str:
        return "fast"

    @property
    def supported_mime_types(self) -> set[str]:
        return {PPTX_MIME}

    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
        progress_callback: Optional[
            Callable[[float, Optional[float], Optional[str]], Awaitable[None]]
        ] = None,
    ) -> ProcessingResult:
        try:
            text, boundaries, slide_count = await run_sync(
                _extract_deck, content, abandon_on_cancel=True
            )
        except Exception as exc:
            raise ProcessorError(f"Presentation parse failed: {exc}") from exc

        return ProcessingResult(
            text=text,
            metadata={
                "slide_count": slide_count,
                SLIDE_BOUNDARIES_KEY: boundaries,
                "text_length": len(text),
                "parse_mode": "markdown",
            },
            processor=self.name,
            success=True,
        )

    async def health_check(self) -> bool:
        try:
            import pptx  # noqa: F401, PLC0415
        except ImportError:
            return False
        return True


def _escape_cell(value: str) -> str:
    """One table cell as markdown-table-safe text."""
    return " ".join(value.replace("|", "\\|").split())


def _render_table(table: Any) -> str:
    """A pptx table as a markdown table. Rows are always rectangular here,
    unlike a spreadsheet's sparse cell range, so no padding is needed."""
    rows = [[_escape_cell(cell.text) for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    lines = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join(["---"] * len(rows[0])) + " |",
    ]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _shape_blocks(shapes: Any) -> list[str]:
    """Text frames and tables in shape order, descending into group shapes."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE  # noqa: PLC0415 -- see _extract_deck

    blocks: list[str] = []
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            blocks.extend(_shape_blocks(shape.shapes))
        elif shape.has_table:
            blocks.append(_render_table(shape.table))
        elif shape.has_text_frame:
            text = shape.text_frame.text.strip()
            if text:
                blocks.append(text)
    return blocks


def _render_slide(slide: Any, index: int) -> str:
    """One slide's text frames and tables, in shape order, plus its notes."""
    blocks = _shape_blocks(slide.shapes)

    # A notes page need not carry a notes placeholder, in which case
    # notes_text_frame is None.
    notes_frame = slide.notes_slide.notes_text_frame if slide.has_notes_slide else None
    if notes_frame is not None:
        notes = notes_frame.text.strip()
        if notes:
            blocks.append(f"**Notes:** {notes}")

    if not blocks:
        return ""
    return f"## Slide {index}\n\n" + "\n\n".join(blocks) + "\n"


def _extract_deck(content: bytes) -> tuple[str, list[dict[str, Any]], int]:
    """Render every slide as markdown. Runs in a worker thread."""
    from pptx import Presentation  # noqa: PLC0415 -- keep the import off the hot path

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        inflated = sum(info.file_size for info in zf.infolist())
    if inflated > MAX_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"uncompressed size {inflated} bytes exceeds "
            f"{MAX_UNCOMPRESSED_BYTES} byte cap"
        )

    prs = Presentation(io.BytesIO(content))

    parts: list[str] = []
    boundaries: list[dict[str, Any]] = []
    offset = 0
    for i, slide in enumerate(prs.slides, start=1):
        body = _render_slide(slide, i)
        if not body:
            continue
        parts.append(body)
        boundaries.append(
            {"slide": i, "start_offset": offset, "end_offset": offset + len(body)}
        )
        offset += len(body)
    return "".join(parts), boundaries, len(prs.slides)
