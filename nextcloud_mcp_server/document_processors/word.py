"""Word documents, read paragraph-by-paragraph rather than through a rendition.

``.docx`` is OOXML, so ``python-docx`` reads it directly -- no LibreOffice or
external service (ADR-038). Legacy ``.doc`` (OLE2) is out of scope: python-docx
cannot open that container.

Optionally (``OFFICE_CAPTION_IMAGES`` + ``DOCLING_API_URL``, ADR-037), an
inline raster picture is captioned by docling-serve and the caption placed
right after the paragraph holding it -- unlike a slide, prose has a reading
order worth keeping.
"""

import io
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from anyio.to_thread import run_sync

from ._ooxml import (
    Picture,
    PictureCaptioner,
    caption_line,
    check_zip_size,
    escape_cell,
    picture_if_eligible,
    picture_metadata,
    render_table,
)
from .base import DocumentProcessor, ProcessingResult, ProcessorError

logger = logging.getLogger(__name__)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
# Block- and run-level wrappers whose content belongs to the document text.
# python-docx's ``iter_inner_content()`` and ``paragraph.text`` skip content
# controls (``w:sdt``), and documents created from Word templates keep their
# text inside them after they are filled in.
_TRANSPARENT = {
    _W + "sdt",
    _W + "sdtContent",
    _W + "customXml",
    _W + "smartTag",
    _W + "ins",
    _W + "hyperlink",
    _W + "fldSimple",
    _W + "r",
}


class DocxProcessor(DocumentProcessor):
    """Extract ``.docx`` as markdown: headings, list items, tables, pictures."""

    def __init__(self, *, captioner: PictureCaptioner | None = None) -> None:
        self._captioner = captioner or PictureCaptioner()

    @property
    def name(self) -> str:
        return "word"

    @property
    def tier(self) -> str:
        return "fast"

    @property
    def supported_mime_types(self) -> set[str]:
        return {DOCX_MIME}

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
            blocks, pictures = await run_sync(
                _extract_document, content, abandon_on_cancel=True
            )
        except Exception as exc:
            raise ProcessorError(f"Word document parse failed: {exc}") from exc

        captions = (
            await self._captioner.caption_all([p for _, p in pictures])
            if self._captioner.enabled
            else None
        )
        if captions:
            # Insert from the back so earlier block indices stay valid.
            for (after, _), caption in reversed(list(zip(pictures, captions))):
                if caption:
                    blocks.insert(after + 1, caption_line(caption))

        text = "\n\n".join(blocks) + ("\n" if blocks else "")
        return ProcessingResult(
            text=text,
            metadata={
                "text_length": len(text),
                "parse_mode": "markdown",
                **picture_metadata(len(pictures), captions),
            },
            processor=self.name,
            success=True,
        )

    async def health_check(self) -> bool:
        try:
            import docx  # noqa: F401, PLC0415
        except ImportError:
            return False
        return True


def _element_text(element: Any) -> str:
    """Visible text under ``element``: runs inside content controls,
    hyperlinks, fields and insertions included; deleted text and the Fallback
    copy of a text box (``mc:AlternateContent``) left out."""
    parts: list[str] = []

    def walk(node: Any) -> None:
        for child in node:
            tag = child.tag
            if tag in (_MC_FALLBACK, _W + "del"):
                continue
            if tag == _W + "t":
                parts.append(child.text or "")
            elif tag == _W + "tab":
                parts.append("\t")
            elif tag in (_W + "br", _W + "cr"):
                parts.append("\n")
            else:
                walk(child)

    walk(element)
    return "".join(parts)


def _cell_text(tc: Any) -> str:
    """A table cell's paragraphs, content controls included."""
    texts = (_element_text(p).strip() for p in tc.iter(_W + "p"))
    return "\n".join(t for t in texts if t)


def _paragraph_markdown(paragraph: Any) -> str:
    """A paragraph as markdown, marking up the two structures a reader
    searches by: headings and list items."""
    text = _element_text(paragraph._p).strip()
    if not text:
        return ""
    style = paragraph.style.name if paragraph.style is not None else ""
    if style == "Title":
        return f"# {text}"
    if style.startswith("Heading "):
        level = style.removeprefix("Heading ")
        if level.isdigit():
            return f"{'#' * min(int(level), 6)} {text}"
    # A list item carries either a List* style or direct numbering (w:numPr),
    # which converters and some Word workflows apply to a plain style.
    p_pr = paragraph._p.pPr
    if style.startswith("List") or (p_pr is not None and p_pr.numPr is not None):
        return f"- {text}"
    return text


def _table_markdown(table: Any) -> str:
    """A docx table as markdown. python-docx repeats a merged cell in every
    grid position it spans, so columns stay aligned; rows are padded only in
    case of a ragged grid (``gridBefore``/``gridAfter``)."""
    rows = [
        [escape_cell(_cell_text(cell._tc)) for cell in row.cells] for row in table.rows
    ]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    return render_table([r + [""] * (width - len(r)) for r in rows])


def _paragraph_pictures(paragraph: Any, part: Any) -> list[Picture]:
    """Eligible pictures anchored in ``paragraph`` (inline or floating)."""
    pictures = []
    for rid in paragraph._p.xpath(".//a:blip/@r:embed"):
        try:
            image = part.related_parts[rid].image
            picture = picture_if_eligible(
                image.content_type, (image.px_width, image.px_height), image.blob
            )
        except Exception as exc:  # noqa: BLE001
            # An unreadable or unrecognized image part costs this picture only.
            logger.debug("Skipping unreadable docx picture: %s", exc)
            continue
        if picture is not None:
            pictures.append(picture)
    return pictures


def _extract_document(content: bytes) -> tuple[list[str], list[tuple[int, Picture]]]:
    """Markdown blocks in body order, plus each eligible picture with the index
    of the block it follows. Runs in a worker thread; captioning is network I/O
    and happens afterwards, in the async ``process()``."""
    from docx import Document  # noqa: PLC0415 -- keep the import off the hot path
    from docx.table import Table  # noqa: PLC0415
    from docx.text.paragraph import Paragraph  # noqa: PLC0415

    check_zip_size(content)
    doc = Document(io.BytesIO(content))

    blocks: list[str] = []
    pictures: list[tuple[int, Picture]] = []

    # ponytail: body only -- headers/footers (usually logos and page numbers)
    # and pictures inside table cells are skipped.
    def walk(container: Any) -> None:
        for element in container:
            if element.tag == _W + "tbl":
                block = _table_markdown(Table(element, doc.part))
                if block:
                    blocks.append(block)
            elif element.tag == _W + "p":
                item = Paragraph(element, doc.part)
                block = _paragraph_markdown(item)
                if block:
                    blocks.append(block)
                for picture in _paragraph_pictures(item, doc.part):
                    # A picture-only paragraph has no block of its own; it
                    # follows whatever came before it (-1 = the start).
                    pictures.append((len(blocks) - 1, picture))
            elif element.tag in _TRANSPARENT:
                # A block-level content control wraps whole paragraphs and
                # tables, which iter_inner_content() would not yield.
                walk(element)

    walk(doc.element.body)
    return blocks, pictures
