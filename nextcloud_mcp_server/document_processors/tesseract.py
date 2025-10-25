"""Document processor using Tesseract OCR (local)."""

import logging
import shutil
from typing import Any, Optional

from .base import DocumentProcessor, ProcessingResult, ProcessorError

logger = logging.getLogger(__name__)

try:
    import io

    import pytesseract
    from PIL import Image

    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False


class TesseractProcessor(DocumentProcessor):
    """Document processor using Tesseract OCR (local).

    This processor runs OCR locally using the Tesseract engine, which is
    faster and more lightweight than cloud-based solutions but requires
    Tesseract to be installed on the system.

    Requirements:
        - tesseract binary installed (e.g., apt install tesseract-ocr)
        - Python packages: pip install pytesseract pillow

    Example:
        processor = TesseractProcessor(default_lang="eng+deu")
        result = await processor.process(image_bytes, "image/jpeg")
    """

    SUPPORTED_TYPES = {
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/bmp",
        "image/gif",
    }

    def __init__(
        self,
        tesseract_cmd: Optional[str] = None,
        default_lang: str = "eng",
    ):
        """Initialize Tesseract processor.

        Args:
            tesseract_cmd: Path to tesseract executable (None = auto-detect)
            default_lang: Default OCR language (e.g., "eng", "deu", "eng+deu")

        Raises:
            ProcessorError: If Tesseract or required packages not available
        """
        if not TESSERACT_AVAILABLE:
            raise ProcessorError(
                "Tesseract processor requires: pip install pytesseract pillow"
            )

        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        elif not shutil.which("tesseract"):
            raise ProcessorError(
                "Tesseract not found in PATH. Install with: apt install tesseract-ocr"
            )

        self.default_lang = default_lang
        logger.info(f"Initialized TesseractProcessor: lang={default_lang}")

    @property
    def name(self) -> str:
        return "tesseract"

    @property
    def supported_mime_types(self) -> set[str]:
        return self.SUPPORTED_TYPES

    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> ProcessingResult:
        """Process image via Tesseract OCR.

        Args:
            content: Image bytes
            content_type: Image MIME type
            filename: Optional filename
            options: Processing options:
                - lang: OCR language(s) (default: from init)
                - config: Tesseract config string

        Returns:
            ProcessingResult with extracted text and metadata

        Raises:
            ProcessorError: If OCR fails
        """
        options = options or {}
        lang = options.get("lang", self.default_lang)
        config = options.get("config", "")

        try:
            # Load image
            image = Image.open(io.BytesIO(content))

            # Run OCR
            text = pytesseract.image_to_string(image, lang=lang, config=config)

            # Get additional data for confidence scores
            data = pytesseract.image_to_data(
                image, lang=lang, output_type=pytesseract.Output.DICT
            )

            # Calculate average confidence
            confidences = [c for c in data["conf"] if c != -1]
            avg_confidence = sum(confidences) / len(confidences) if confidences else 0

            metadata = {
                "text_length": len(text),
                "language": lang,
                "image_size": image.size,
                "image_mode": image.mode,
                "confidence": round(avg_confidence, 2),
                "words_detected": len([c for c in data["conf"] if c != -1]),
            }

            logger.debug(
                f"Tesseract OCR completed: {len(text)} chars, "
                f"confidence={avg_confidence:.1f}%"
            )

            return ProcessingResult(
                text=text.strip(),
                metadata=metadata,
                processor=self.name,
                success=True,
            )

        except Exception as e:
            logger.error(f"Tesseract processing failed: {e}")
            raise ProcessorError(f"OCR failed: {str(e)}") from e

    async def health_check(self) -> bool:
        """Check if Tesseract is available.

        Returns:
            True if Tesseract is installed and working
        """
        try:
            pytesseract.get_tesseract_version()
            return True
        except Exception:
            return False
