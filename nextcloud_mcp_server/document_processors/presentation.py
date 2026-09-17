"""Presentations, read shape-by-shape rather than through a PDF rendition.

``.pptx`` is already OOXML (a zip of XML parts), so ``python-pptx`` reads it
directly -- no LibreOffice/``soffice`` dependency, unlike the ``.doc``/``.docx``
rendition route in ``office.py``. Legacy ``.ppt`` (OLE2) is out of scope here
for the same reason ``office.py``'s ``DOC_MIME_TYPES`` excludes legacy binary
formats from the formats it can read directly: python-pptx cannot open the
OLE2 container, only the OOXML one.
"""

import io
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from anyio.to_thread import run_sync

from .base import DocumentProcessor, ProcessingResult, ProcessorError

logger = logging.getLogger(__name__)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Per-slide spans into the joined text: {"slide", "start_offset", "end_offset"}.
# The presentation counterpart to a PDF's ``page_boundaries`` / a spreadsheet's
# ``sheet_boundaries`` -- lets a chunk be attributed to the slide it came from.
SLIDE_BOUNDARIES_KEY = "slide_boundaries"


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
            text, boundaries, slide_count = await run_sync(_extract_deck, content)
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
    """One table cell as markdown-table-safe text -- see ``spreadsheet._escape``."""
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


def _render_slide(slide: Any, index: int) -> str:
    """One slide's text frames and tables, in shape order, plus its notes."""
    blocks: list[str] = []
    for shape in slide.shapes:
        if shape.has_table:
            blocks.append(_render_table(shape.table))
        elif shape.has_text_frame:
            text = shape.text_frame.text.strip()
            if text:
                blocks.append(text)

    if slide.has_notes_slide:
        notes = slide.notes_slide.notes_text_frame.text.strip()
        if notes:
            blocks.append(f"**Notes:** {notes}")

    if not blocks:
        return ""
    return f"## Slide {index}\n\n" + "\n\n".join(blocks) + "\n"


def _extract_deck(content: bytes) -> tuple[str, list[dict[str, Any]], int]:
    """Render every slide as markdown. Runs in a worker thread."""
    from pptx import Presentation  # noqa: PLC0415 -- keep the import off the hot path

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
