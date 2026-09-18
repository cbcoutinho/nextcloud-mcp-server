"""Presentations, read shape-by-shape rather than through a PDF rendition.

``.pptx`` is already OOXML (a zip of XML parts), so ``python-pptx`` reads it
directly -- no LibreOffice/``soffice`` dependency. Legacy ``.ppt`` (OLE2) is
out of scope: python-pptx cannot open the OLE2 container, only the OOXML one.

Optionally (``PPTX_CAPTION_IMAGES`` + ``DOCLING_API_URL``, see ADR-037), a
raster picture pasted onto a slide is captioned by sending it to the same
docling-serve instance the images-only ``DoclingProcessor`` uses, and the
caption is appended to that slide's text. This only reaches actual pictures --
native vector diagrams (SmartArt, freeform/connector shapes) have no image
part to send and are unaffected; python-pptx has no rendering engine to turn
those into pixels.
"""

import io
import logging
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from anyio.to_thread import run_sync

from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .docling_serve import DOCLING_IMAGE_TYPES, convert_file

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

# A picture below this on either axis (native pixels, not on-slide display
# size) is almost always decorative -- a logo, a bullet icon, a divider -- not
# a diagram worth a docling round trip. Keeps a capped captioning budget spent
# on content (see ADR-037).
MIN_CAPTION_PICTURE_PX = 80


@dataclass
class _Picture:
    """One raster picture shape eligible for docling captioning."""

    content_type: str
    blob: bytes


@dataclass
class _SlideData:
    """One slide's extracted blocks, notes and picture shapes, pre-captioning."""

    index: int
    blocks: list[str]
    pictures: list[_Picture] = field(default_factory=list)
    notes: Optional[str] = None


class PptxProcessor(DocumentProcessor):
    """Extract ``.pptx`` as one markdown section per slide."""

    def __init__(
        self,
        *,
        caption_images: bool = False,
        docling_api_url: str | None = None,
        caption_max_images: int = 8,
        caption_timeout: float = 15.0,
        docling_pipeline: str = "standard",
        docling_vlm_preset: str | None = None,
        docling_ocr_lang: list[str] | None = None,
    ) -> None:
        # Explicit opt-in beyond a bare docling_api_url, deliberately (mirrors
        # document_ocr_provider="docling" needing its own selection): a
        # deployment that only wants docling for scanned-PDF OCR should not
        # start captioning every picture in every presentation for free.
        self._should_caption = bool(caption_images and docling_api_url)
        if caption_images and not docling_api_url:
            logger.warning(
                "PPTX_CAPTION_IMAGES=true but DOCLING_API_URL is unset; "
                "picture captioning stays disabled"
            )
        self._docling_api_url = docling_api_url
        self._caption_max_images = caption_max_images
        self._caption_timeout = caption_timeout
        self._docling_pipeline = docling_pipeline
        self._docling_vlm_preset = docling_vlm_preset
        self._docling_ocr_lang = docling_ocr_lang

    @property
    def name(self) -> str:
        return "presentation"

    @property
    def tier(self) -> str:
        return "fast"

    @property
    def supported_mime_types(self) -> set[str]:
        return {PPTX_MIME}

    async def _caption_picture(self, picture: _Picture) -> str | None:
        """A short markdown description of one picture, or ``None`` on failure.

        A single picture failing (docling-serve error, timeout, unsupported
        content) costs that picture's caption, never the rest of the deck --
        mirrors how a missing OCR backend degrades ``_ocr_note`` rather than
        failing the parse.
        """
        assert self._docling_api_url is not None  # guarded by _should_caption
        try:
            document = await convert_file(
                self._docling_api_url,
                picture.blob,
                picture.content_type,
                to_formats=["md"],
                pipeline=self._docling_pipeline,
                vlm_pipeline_preset=self._docling_vlm_preset,
                ocr_lang=self._docling_ocr_lang,
                timeout=self._caption_timeout,
            )
        except ProcessorError as exc:
            logger.warning("PPTX picture captioning failed: %s", exc)
            return None
        caption = (
            document.get("md_content") or document.get("text_content") or ""
        ).strip()
        return caption or None

    async def _caption_all_pictures(self, slides: list["_SlideData"]) -> int:
        """Caption up to ``_caption_max_images`` pictures across ``slides``,
        appending a caption block to each slide whose picture succeeds.
        Returns how many were captioned.
        """
        budget = self._caption_max_images
        captioned = 0
        for slide in slides:
            for picture in slide.pictures:
                if budget <= 0:
                    return captioned
                budget -= 1
                caption = await self._caption_picture(picture)
                if caption:
                    slide.blocks.append(f"*Image: {caption}*")
                    captioned += 1
        return captioned

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
            slides, slide_count = await run_sync(
                _extract_deck, content, abandon_on_cancel=True
            )
        except Exception as exc:
            raise ProcessorError(f"Presentation parse failed: {exc}") from exc

        pictures_found = sum(len(slide.pictures) for slide in slides)
        pictures_captioned = (
            await self._caption_all_pictures(slides) if self._should_caption else None
        )

        text, boundaries = _assemble_text(slides)

        metadata: dict[str, Any] = {
            "slide_count": slide_count,
            SLIDE_BOUNDARIES_KEY: boundaries,
            "text_length": len(text),
            "parse_mode": "markdown",
            # Present even when captioning is off/unconfigured -- on its own
            # this already tells a caller "there are pictures you're not
            # seeing" (see document_parser._pptx_caption_note).
            "pptx_pictures_found": pictures_found,
        }
        if pictures_captioned is not None:
            metadata["pptx_pictures_captioned"] = pictures_captioned

        return ProcessingResult(
            text=text,
            metadata=metadata,
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


def _eligible_picture(shape: Any) -> Optional[_Picture]:
    """``shape``'s image, if it is worth a docling captioning round trip.

    Skips content types docling's image processor does not accept (e.g. the
    EMF/WMF vector pastes common for copied diagrams -- ``DOCLING_IMAGE_TYPES``
    is raster-only) and pictures too small on their native axis to be more than
    a logo or bullet icon (``MIN_CAPTION_PICTURE_PX``), so a capped budget is
    spent on content instead of decoration.
    """
    try:
        image = shape.image
    except Exception as exc:  # noqa: BLE001
        # A corrupt/unreadable image part costs this picture, not the deck.
        logger.debug("Skipping unreadable picture shape: %s", exc)
        return None
    if image.content_type not in DOCLING_IMAGE_TYPES:
        return None
    width_px, height_px = image.size
    if width_px < MIN_CAPTION_PICTURE_PX or height_px < MIN_CAPTION_PICTURE_PX:
        return None
    return _Picture(content_type=image.content_type, blob=image.blob)


def _shape_blocks(shapes: Any) -> tuple[list[str], list[_Picture]]:
    """Text frames, tables and eligible pictures in shape order, descending
    into group shapes."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE  # noqa: PLC0415 -- see _extract_deck

    blocks: list[str] = []
    pictures: list[_Picture] = []
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            sub_blocks, sub_pictures = _shape_blocks(shape.shapes)
            blocks.extend(sub_blocks)
            pictures.extend(sub_pictures)
        elif shape.has_table:
            blocks.append(_render_table(shape.table))
        elif shape.has_text_frame:
            text = shape.text_frame.text.strip()
            if text:
                blocks.append(text)
        elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            picture = _eligible_picture(shape)
            if picture is not None:
                pictures.append(picture)
    return blocks, pictures


def _render_slide_markdown(slide: _SlideData) -> str:
    """One slide's blocks (text, tables, resolved image captions) plus notes."""
    blocks = list(slide.blocks)
    if slide.notes:
        blocks.append(f"**Notes:** {slide.notes}")
    if not blocks:
        return ""
    return f"## Slide {slide.index}\n\n" + "\n\n".join(blocks) + "\n"


def _assemble_text(slides: list[_SlideData]) -> tuple[str, list[dict[str, Any]]]:
    """Join every non-empty slide's markdown and its ``slide_boundaries``
    span, in one pass so the offsets always index the returned text exactly."""
    parts: list[str] = []
    boundaries: list[dict[str, Any]] = []
    offset = 0
    for slide in slides:
        body = _render_slide_markdown(slide)
        if not body:
            continue
        parts.append(body)
        boundaries.append(
            {
                "slide": slide.index,
                "start_offset": offset,
                "end_offset": offset + len(body),
            }
        )
        offset += len(body)
    return "".join(parts), boundaries


def _extract_deck(content: bytes) -> tuple[list[_SlideData], int]:
    """Parse every slide's blocks, notes and picture shapes. Runs in a worker
    thread; picture captioning is network I/O and happens afterwards, in the
    async ``process()``."""
    from pptx import Presentation  # noqa: PLC0415 -- keep the import off the hot path

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        inflated = sum(info.file_size for info in zf.infolist())
    if inflated > MAX_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"uncompressed size {inflated} bytes exceeds "
            f"{MAX_UNCOMPRESSED_BYTES} byte cap"
        )

    prs = Presentation(io.BytesIO(content))

    slides: list[_SlideData] = []
    for i, slide in enumerate(prs.slides, start=1):
        blocks, pictures = _shape_blocks(slide.shapes)

        # A notes page need not carry a notes placeholder, in which case
        # notes_text_frame is None.
        notes_frame = (
            slide.notes_slide.notes_text_frame if slide.has_notes_slide else None
        )
        notes = notes_frame.text.strip() if notes_frame is not None else ""

        slides.append(
            _SlideData(index=i, blocks=blocks, pictures=pictures, notes=notes or None)
        )
    return slides, len(prs.slides)
