"""Document processor using Unstructured.io API."""

import io
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import anyio
import httpx

from .base import DocumentProcessor, ProcessingResult, ProcessorError

logger = logging.getLogger(__name__)


class UnstructuredProcessor(DocumentProcessor):
    """Document processor using Unstructured.io API.

    The Unstructured API provides document parsing capabilities for various formats
    including PDF, DOCX, images with OCR, and more.

    API Documentation: https://docs.unstructured.io/api-reference/api-services/api-parameters
    """

    # Supported MIME types for Unstructured
    SUPPORTED_TYPES = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/rtf",
        "text/rtf",
        "application/vnd.oasis.opendocument.text",
        "application/epub+zip",
        "message/rfc822",
        "application/vnd.ms-outlook",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/bmp",
    }

    def __init__(
        self,
        api_url: str,
        timeout: int = 120,
        default_strategy: str = "auto",
        default_languages: Optional[list[str]] = None,
        progress_interval: int = 10,
    ):
        """Initialize Unstructured processor.

        Args:
            api_url: Unstructured API endpoint
            timeout: Request timeout in seconds (default: 120)
            default_strategy: Default parsing strategy - "auto", "fast", or "hi_res"
            default_languages: Default OCR language codes (e.g., ["eng", "deu"])
            progress_interval: Seconds between progress updates (default: 10)
        """
        self.api_url = api_url
        self.timeout = timeout
        self.default_strategy = default_strategy
        self.default_languages = default_languages or ["eng"]
        self.progress_interval = progress_interval

        logger.info(
            f"Initialized UnstructuredProcessor: {api_url}, "
            f"strategy={default_strategy}, languages={self.default_languages}, "
            f"progress_interval={progress_interval}s"
        )

    @property
    def name(self) -> str:
        return "unstructured"

    @property
    def supported_mime_types(self) -> set[str]:
        return self.SUPPORTED_TYPES

    async def _run_progress_poller(
        self,
        stop_event: anyio.Event,
        progress_callback: Callable[
            [float, Optional[float], Optional[str]], Awaitable[None]
        ],
        start_time: float,
    ):
        """Run progress poller that reports status every N seconds.

        Args:
            stop_event: Event to signal when processing is complete
            progress_callback: Async callback to report progress
            start_time: Time when processing started (from time.time())
        """
        logger.debug("Starting progress poller")
        while not stop_event.is_set():
            try:
                # Wait for the event to be set, with a timeout equal to progress_interval
                with anyio.fail_after(self.progress_interval):
                    await stop_event.wait()
                # If wait() finished, the event was set (processing complete)
                break
            except TimeoutError:
                # Timeout occurred - time to send a progress update
                if not stop_event.is_set():  # Double-check in case of race condition
                    elapsed = int(time.time() - start_time)
                    message = (
                        f"Processing document with unstructured... ({elapsed}s elapsed)"
                    )
                    try:
                        await progress_callback(
                            progress=float(elapsed),
                            total=None,  # Unknown total duration
                            message=message,
                        )
                        logger.debug(f"Progress update sent: {elapsed}s elapsed")
                    except Exception as e:
                        logger.warning(f"Failed to send progress update: {e}")
        logger.debug("Progress poller stopped")

    async def _make_api_request(
        self,
        content: bytes,
        content_type: str,
        filename: Optional[str],
        strategy: str,
        languages: list[str],
        extract_image_block_types: Optional[list[str]],
    ) -> ProcessingResult:
        """Make the actual API request to Unstructured.

        Args:
            content: Document bytes
            content_type: MIME type
            filename: Optional filename
            strategy: Processing strategy
            languages: OCR languages
            extract_image_block_types: Image element types to extract

        Returns:
            ProcessingResult with extracted text and metadata

        Raises:
            ProcessorError: If processing fails
        """
        # Prepare multipart request
        files = {
            "files": (
                filename or "document",
                io.BytesIO(content),
                content_type or "application/octet-stream",
            )
        }

        data = {
            "strategy": strategy,
            "languages": ",".join(languages),
        }

        if extract_image_block_types:
            data["extract_image_block_types"] = ",".join(extract_image_block_types)

        logger.debug(
            f"Processing with Unstructured API: strategy={strategy}, languages={languages}"
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.api_url}/general/v0/general",
                    files=files,
                    data=data,
                )
                response.raise_for_status()

                # Parse response
                elements = response.json()

                # Extract text and metadata
                texts = []
                element_types: dict[str, int] = {}

                for element in elements:
                    if "text" in element and element["text"]:
                        texts.append(element["text"])

                    el_type = element.get("type", "unknown")
                    element_types[el_type] = element_types.get(el_type, 0) + 1

                parsed_text = "\n\n".join(texts)

                metadata = {
                    "element_count": len(elements),
                    "text_length": len(parsed_text),
                    "element_types": element_types,
                    "strategy": strategy,
                    "languages": languages,
                }

                logger.debug(
                    f"Successfully processed: {len(elements)} elements, "
                    f"{len(parsed_text)} characters"
                )

                return ProcessingResult(
                    text=parsed_text,
                    metadata=metadata,
                    processor=self.name,
                    success=True,
                )

        except httpx.HTTPError as e:
            logger.error(f"Unstructured API HTTP error: {e}")
            raise ProcessorError(f"HTTP error: {str(e)}") from e
        except Exception as e:
            logger.error(f"Unstructured API processing failed: {e}")
            raise ProcessorError(f"Processing failed: {str(e)}") from e

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
        """Process document via Unstructured API.

        Args:
            content: Document bytes
            content_type: MIME type
            filename: Optional filename for format detection
            options: Processing options:
                - strategy: "auto", "fast", or "hi_res" (default: from init)
                - languages: List of language codes (default: from init)
                - extract_image_block_types: Types of image elements to extract
            progress_callback: Optional async callback for progress updates

        Returns:
            ProcessingResult with extracted text and metadata

        Raises:
            ProcessorError: If processing fails
        """
        options = options or {}

        # Extract options with defaults
        strategy = options.get("strategy", self.default_strategy)
        languages = options.get("languages", self.default_languages)
        extract_image_block_types = options.get("extract_image_block_types")

        # If no progress callback, just make the request directly
        if progress_callback is None:
            return await self._make_api_request(
                content=content,
                content_type=content_type,
                filename=filename,
                strategy=strategy,
                languages=languages,
                extract_image_block_types=extract_image_block_types,
            )

        # With progress callback: run API request + progress poller concurrently
        stop_event = anyio.Event()
        start_time = time.time()
        result = None

        async def capture_result():
            nonlocal result
            try:
                result = await self._make_api_request(
                    content=content,
                    content_type=content_type,
                    filename=filename,
                    strategy=strategy,
                    languages=languages,
                    extract_image_block_types=extract_image_block_types,
                )
            finally:
                # Signal poller to stop after API request completes
                stop_event.set()

        # Run both tasks concurrently using anyio task groups
        async with anyio.create_task_group() as tg:
            tg.start_soon(capture_result)
            tg.start_soon(
                self._run_progress_poller, stop_event, progress_callback, start_time
            )

        return result

    async def health_check(self) -> bool:
        """Check if Unstructured API is available.

        Returns:
            True if API is healthy, False otherwise
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.api_url}/healthcheck")
                return response.status_code == 200
        except Exception as e:
            logger.warning(f"Unstructured health check failed: {e}")
            return False
