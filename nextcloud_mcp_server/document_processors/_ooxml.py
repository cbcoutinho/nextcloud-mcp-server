"""Shared pieces of the native OOXML readers (``.pptx``/``.docx``/``.xlsx``).

Each format walks its own package, but all of them need the same zip-bomb
guard, the same markdown table rendering, and the same optional docling-serve
captioning of embedded raster pictures (ADR-037).
"""

import io
import logging
import zipfile
from dataclasses import dataclass

from .base import ProcessorError
from .docling_serve import DOCLING_IMAGE_TYPES, convert_file

logger = logging.getLogger(__name__)

# The OOXML readers inflate every package part into memory, so bound the total
# *declared* uncompressed size before opening it (zipfile will not inflate past
# a member's declared size, so the header cannot lie its way around this). The
# download ceiling only caps the compressed bytes; this stops a zip bomb.
# ponytail: fixed cap, make it a setting if real documents hit it.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

# A picture below this on either axis (native pixels, not display size) is
# almost always decorative -- a logo, a bullet icon, a divider -- not a diagram
# worth a docling round trip. Keeps a capped captioning budget spent on
# content (see ADR-037).
MIN_CAPTION_PICTURE_PX = 80


def check_zip_size(content: bytes) -> None:
    """Refuse a package whose declared uncompressed size exceeds the cap."""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        inflated = sum(info.file_size for info in zf.infolist())
    if inflated > MAX_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"uncompressed size {inflated} bytes exceeds "
            f"{MAX_UNCOMPRESSED_BYTES} byte cap"
        )


def escape_cell(value: str) -> str:
    """One table cell as markdown-table-safe text: a literal ``|`` would end
    the column early, a newline would end the row."""
    return " ".join(value.replace("|", "\\|").split())


def render_table(rows: list[list[str]]) -> str:
    """Rectangular rows of already-escaped cells as a markdown table, the
    first row as header."""
    if not rows:
        return ""
    lines = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join(["---"] * len(rows[0])) + " |",
    ]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


@dataclass
class Picture:
    """One raster picture eligible for docling captioning."""

    content_type: str
    blob: bytes


def picture_if_eligible(
    content_type: str, size_px: tuple[int, int], blob: bytes
) -> Picture | None:
    """A ``Picture`` if it is worth a docling captioning round trip.

    Skips content types docling's image processor does not accept (e.g. the
    EMF/WMF vector pastes common for copied diagrams -- ``DOCLING_IMAGE_TYPES``
    is raster-only) and pictures too small to be more than a logo or bullet
    icon (``MIN_CAPTION_PICTURE_PX``).
    """
    if content_type not in DOCLING_IMAGE_TYPES:
        return None
    width_px, height_px = size_px
    if width_px < MIN_CAPTION_PICTURE_PX or height_px < MIN_CAPTION_PICTURE_PX:
        return None
    return Picture(content_type=content_type, blob=blob)


class PictureCaptioner:
    """Captions pictures through docling-serve, up to ``max_images`` per file.

    Stateless across files: the cap is applied per ``caption_all`` call, so one
    instance lives on the processor.
    """

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
        # start captioning every picture in every document for free.
        self.enabled = bool(caption_images and docling_api_url)
        if caption_images and not docling_api_url:
            logger.warning(
                "OFFICE_CAPTION_IMAGES=true but DOCLING_API_URL is unset; "
                "picture captioning stays disabled"
            )
        self._docling_api_url = docling_api_url
        self.max_images = caption_max_images
        self._timeout = caption_timeout
        self._pipeline = docling_pipeline
        self._vlm_preset = docling_vlm_preset
        self._ocr_lang = docling_ocr_lang

    async def caption(self, picture: Picture) -> str | None:
        """A short markdown description of one picture, or ``None`` on failure.

        A single picture failing (docling-serve error, timeout, unsupported
        content) costs that picture's caption, never the rest of the file --
        mirrors how a missing OCR backend degrades ``_ocr_note`` rather than
        failing the parse.
        """
        assert self._docling_api_url is not None  # guarded by enabled
        try:
            document = await convert_file(
                self._docling_api_url,
                picture.blob,
                picture.content_type,
                to_formats=["md"],
                pipeline=self._pipeline,
                vlm_pipeline_preset=self._vlm_preset,
                ocr_lang=self._ocr_lang,
                timeout=self._timeout,
            )
        except ProcessorError as exc:
            logger.warning("Picture captioning failed: %s", exc)
            return None
        caption = (
            document.get("md_content") or document.get("text_content") or ""
        ).strip()
        return caption or None

    async def caption_all(self, pictures: list[Picture]) -> list[str | None]:
        """Caption ``pictures`` in order, sequentially (ADR-037 D2), up to
        ``max_images``; pictures past the cap get ``None``."""
        captions: list[str | None] = []
        for i, picture in enumerate(pictures):
            captions.append(
                await self.caption(picture) if i < self.max_images else None
            )
        return captions


def caption_line(caption: str) -> str:
    """How a picture caption appears in the extracted markdown."""
    return f"*Image: {caption}*"


def picture_metadata(found: int, captions: list[str | None] | None) -> dict:
    """``pictures_found`` always -- on its own already a signal that there is
    content the caller is not seeing -- and ``pictures_captioned`` only when
    captioning was attempted (see ``document_parser._picture_caption_note``)."""
    metadata: dict = {"pictures_found": found}
    if captions is not None:
        metadata["pictures_captioned"] = sum(1 for c in captions if c)
    return metadata
