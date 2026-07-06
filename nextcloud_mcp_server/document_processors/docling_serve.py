"""Document processing via a docling-serve HTTP instance.

`docling <https://github.com/docling-project/docling>`_ parses PDFs, images and
office formats with strong OCR (incl. photographed / scanned / handwritten text
where ``unstructured`` struggles). This module talks to an external
`docling-serve <https://github.com/docling-project/docling-serve>`_ instance over
HTTP (``DOCLING_API_URL``) -- no heavy ML dependencies live in the MCP server.

Two touchpoints share the :func:`convert_file` client here:

  * :class:`DoclingProcessor` -- the images-only processor registered on the
    ``find_processor`` priority path (``app.py``). Its ``process()`` also handles
    PDFs/office formats so it can be *force-selected* by name
    (``registry.process(processor_name="docling")``) to re-parse a text-layer PDF
    (tables / partial text) that the classifier would otherwise leave to the fast
    tier. It is deliberately NOT auto-selected for PDFs (``supported_mime_types``
    is images-only), so enabling docling never reroutes every PDF through it.
  * ``document_processors.ocr._DoclingServeBackend`` -- the PDF OCR-tier backend
    (``DOCUMENT_OCR_PROVIDER=docling``) for scanned/no-text-layer PDFs.

docling-serve API (v1): ``POST /v1/convert/file`` (multipart, synchronous) returns
``{"document": {"md_content", "text_content", "json_content", ...}, "status": ...}``.
``GET /health`` is the liveness probe. The synchronous endpoint has a server-side
ceiling (~2 min); async submit/poll is future work.
"""

import io
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import httpx

from .base import DocumentProcessor, ProcessingResult, ProcessorError

logger = logging.getLogger(__name__)

# MIME types the standalone DoclingProcessor auto-serves (find_processor path).
# PDFs are intentionally excluded here (see module docstring) but are still
# handled by process() when the processor is force-selected by name.
DOCLING_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/bmp",
    "image/gif",
    "image/webp",
}

# docling-serve conversion statuses treated as usable output.
_OK_STATUSES = {"success", "partial_success"}

# Cap the connect phase separately from the (long) read timeout so an unreachable
# docling-serve host fails fast instead of hanging for the full DOCLING_TIMEOUT.
# Mirrors ocr._OCR_CONNECT_TIMEOUT_SECONDS for the sibling gateway/Mistral backends.
_CONNECT_TIMEOUT_SECONDS = 10.0


def _from_format_for_mime(content_type: str | None) -> str | None:
    """docling ``from_formats`` hint for a MIME type, or ``None`` to let docling
    infer from the filename (office formats etc.)."""
    if not content_type:
        return None
    base = content_type.split(";")[0].strip().lower()
    if base in DOCLING_IMAGE_TYPES:
        return "image"
    if base == "application/pdf":
        return "pdf"
    return None


def _document_text(document: dict[str, Any]) -> str:
    """Best available flat text from a docling ``document`` payload.

    Prefers markdown (keeps table structure -- the reason to force docling on a
    text-layer PDF) and falls back to plain text.
    """
    return document.get("md_content") or document.get("text_content") or ""


def docling_pages(json_content: Any) -> list[tuple[int, str]]:
    """Group a ``DoclingDocument.json_content`` into ``[(page_index, text)]``.

    docling-serve returns flat ``md_content``/``text_content`` with no per-page
    split, but ``json_content.texts[].prov[].page_no`` carries provenance page
    numbers (PDF). Group text items by that 1-based page number (dropping items
    with no provenance) and return 0-based ``(index, text)`` tuples ready for
    :func:`document_processors.ocr._pages_to_text`. Empty when no page provenance
    is available -- the caller then synthesizes a single whole-text page.
    """
    if not isinstance(json_content, dict):
        return []
    texts = json_content.get("texts") or []
    by_page: dict[int, list[str]] = {}
    for item in texts:
        if not isinstance(item, dict):
            continue
        prov = item.get("prov") or []
        page_no = prov[0].get("page_no") if prov and isinstance(prov[0], dict) else None
        text = item.get("text") or ""
        if not isinstance(page_no, int) or not text:
            continue
        by_page.setdefault(page_no, []).append(text)
    # page_no is 1-based; _pages_to_text expects 0-based indices (it emits page =
    # index + 1). Sort so boundaries index in document order.
    return [
        (page_no - 1, "\n\n".join(parts)) for page_no, parts in sorted(by_page.items())
    ]


async def convert_file(
    api_url: str,
    content: bytes,
    content_type: str | None,
    *,
    filename: str | None = None,
    to_formats: list[str],
    do_ocr: bool = True,
    ocr_lang: list[str] | None = None,
    pipeline: str | None = None,
    vlm_pipeline_preset: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """POST one document to docling-serve ``/v1/convert/file`` and return its
    ``document`` payload (``md_content``/``text_content``/``json_content``/...).

    ``pipeline="vlm"`` drives docling-serve's Vision-LLM pipeline (optionally with a
    server-defined ``vlm_pipeline_preset``); the default (``None``/``"standard"``)
    requests the classic layout+OCR pipeline with byte-identical form fields to the
    pre-VLM client. The response shape is the same for both pipelines.

    Raises :class:`ProcessorError` on HTTP error, a non-usable ``status``
    (anything but success/partial_success), or empty output -- so callers get a
    single failure type to map to their fallback.
    """
    files = {
        "files": (
            filename or "document",
            io.BytesIO(content),
            content_type or "application/octet-stream",
        )
    }
    # docling-serve reads repeated multipart fields for list params; httpx emits a
    # part per list item. Booleans go as lowercase strings for FastAPI coercion.
    data: dict[str, Any] = {"to_formats": list(to_formats)}
    from_format = _from_format_for_mime(content_type)
    if from_format:
        data["from_formats"] = [from_format]
    if pipeline == "vlm":
        # VLM pipeline: docling-serve defaults an absent ``pipeline`` to "standard",
        # so it must be requested explicitly. The classic OCR engine isn't used, so
        # do_ocr/ocr_lang are inert and omitted. Select the (server-defined) preset
        # when given, and keep base64 page images out of the markdown for lean text.
        data["pipeline"] = "vlm"
        if vlm_pipeline_preset:
            data["vlm_pipeline_preset"] = vlm_pipeline_preset
        data["image_export_mode"] = "placeholder"
    else:
        # Standard pipeline (default): byte-identical to pre-VLM requests -- no
        # ``pipeline`` field is emitted, so existing deployments are unaffected.
        data["do_ocr"] = "true" if do_ocr else "false"
        if ocr_lang:
            data["ocr_lang"] = list(ocr_lang)

    url = f"{api_url.rstrip('/')}/v1/convert/file"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=_CONNECT_TIMEOUT_SECONDS)
        ) as client:
            response = await client.post(url, files=files, data=data)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as e:
        logger.error("docling-serve HTTP error: %s", e)
        raise ProcessorError(f"HTTP error: {e}") from e
    except ValueError as e:
        # response.json() raises ValueError (JSONDecodeError) on a non-JSON body --
        # e.g. an HTML error page from an intermediary proxy. Honor the documented
        # single-failure-type (ProcessorError) contract rather than leaking it.
        logger.error("docling-serve returned a non-JSON body: %s", e)
        raise ProcessorError(f"invalid JSON response: {e}") from e

    # A valid-JSON but non-object body (a bare list/string from a misbehaving proxy)
    # would make the .get() calls below raise AttributeError; fail as ProcessorError.
    if not isinstance(body, dict):
        raise ProcessorError(
            f"unexpected docling response shape: {type(body).__name__}"
        )

    status = str(body.get("status") or "").lower()
    document = body.get("document") or {}
    if status and status not in _OK_STATUSES:
        errors = body.get("errors") or []
        raise ProcessorError(f"docling conversion status={status!r} errors={errors}")
    if not _document_text(document):
        raise ProcessorError(f"docling returned no text (status={status!r})")
    return document


async def health(api_url: str, *, timeout: float = 5.0) -> bool:
    """docling-serve liveness (``GET /health`` -> 200)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{api_url.rstrip('/')}/health")
            return response.status_code == 200
    except Exception as e:  # noqa: BLE001 -- health probe never raises
        logger.warning("docling health check failed: %s", e)
        return False


class DoclingProcessor(DocumentProcessor):
    """Images-only auto processor backed by a docling-serve instance.

    Auto-selected for images (``find_processor`` priority path); ``process()`` also
    handles PDFs/office formats so it can be force-selected by name to re-parse a
    text-layer PDF. Extracts markdown (keeps tables) via ``/v1/convert/file``.
    """

    def __init__(
        self,
        api_url: str,
        *,
        timeout: int = 120,
        ocr_lang: list[str] | None = None,
        do_ocr: bool = True,
        pipeline: str = "standard",
        vlm_preset: str | None = None,
        progress_interval: int = 10,
    ) -> None:
        self.api_url = api_url
        self.timeout = timeout
        self.ocr_lang = ocr_lang or ["en", "de"]
        self.do_ocr = do_ocr
        self.pipeline = pipeline
        self.vlm_preset = vlm_preset
        self.progress_interval = progress_interval
        logger.info(
            "Initialized DoclingProcessor: %s, pipeline=%s, ocr_lang=%s, do_ocr=%s",
            api_url,
            pipeline,
            self.ocr_lang,
            do_ocr,
        )
        # DoclingProcessor is the INTERACTIVE path (nc_webdav_read_file on images /
        # force_processor="docling"). Under VLM a convert is slow (~30-150s/page) and
        # blocks the tool call for up to DOCLING_TIMEOUT, which can exceed an MCP
        # client's own timeout. Don't inflate DOCLING_TIMEOUT to "fix" this: for bulk
        # VLM use the async ingest path (DOCUMENT_OCR_PROVIDER=docling, its own
        # DOCUMENT_OCR_TIMEOUT_SECONDS), and set DOCUMENT_READ_TIMEOUT_SECONDS for a
        # graceful base64 fallback on interactive reads (ADR-032).
        if pipeline == "vlm":
            logger.warning(
                "DOCLING_PIPELINE=vlm makes nc_webdav_read_file a slow synchronous "
                "convert (VLM ~30-150s/page, up to DOCLING_TIMEOUT=%ss) that can "
                "exceed MCP client timeouts; prefer the async ingest path for bulk "
                "and set DOCUMENT_READ_TIMEOUT_SECONDS to bound interactive reads",
                timeout,
            )

    @property
    def name(self) -> str:
        return "docling"

    @property
    def tier(self) -> str:
        # A single-shot extraction; images have no escalation ladder. Kept off the
        # PDF tier ladder ("fast"/"structured"/"ocr") on purpose -- supported_mime_types
        # is images-only, so it is never auto-selected for PDFs.
        return "fast"

    @property
    def supported_mime_types(self) -> set[str]:
        return DOCLING_IMAGE_TYPES

    async def _convert(
        self, content: bytes, content_type: str, filename: str | None
    ) -> ProcessingResult:
        document = await convert_file(
            self.api_url,
            content,
            content_type,
            filename=filename,
            to_formats=["md"],
            do_ocr=self.do_ocr,
            ocr_lang=self.ocr_lang,
            pipeline=self.pipeline,
            vlm_pipeline_preset=self.vlm_preset,
            timeout=self.timeout,
        )
        text = _document_text(document)
        return ProcessingResult(
            text=text,
            metadata={
                "parsing_method": "docling",
                "docling_pipeline": self.pipeline,
                "text_length": len(text),
            },
            processor=self.name,
        )

    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: str | None = None,
        options: dict[str, Any] | None = None,
        progress_callback: (
            Callable[[float, float | None, str | None], Awaitable[None]] | None
        ) = None,
    ) -> ProcessingResult:
        if progress_callback is None:
            return await self._convert(content, content_type, filename)

        # Report progress heartbeats while the (potentially slow) OCR conversion
        # runs -- mirrors UnstructuredProcessor.
        stop_event = anyio.Event()
        start_time = time.time()
        result: ProcessingResult | None = None

        async def capture_result() -> None:
            nonlocal result
            try:
                result = await self._convert(content, content_type, filename)
            finally:
                stop_event.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(capture_result)
            tg.start_soon(
                self._run_progress_poller, stop_event, progress_callback, start_time
            )

        return result  # type: ignore[return-value]

    async def _run_progress_poller(
        self,
        stop_event: anyio.Event,
        progress_callback: Callable[[float, float | None, str | None], Awaitable[None]],
        start_time: float,
    ) -> None:
        while not stop_event.is_set():
            try:
                with anyio.fail_after(self.progress_interval):
                    await stop_event.wait()
                break
            except TimeoutError:
                if stop_event.is_set():
                    break
                elapsed = int(time.time() - start_time)
                try:
                    await progress_callback(
                        float(elapsed),
                        None,
                        f"Processing document with docling... ({elapsed}s elapsed)",
                    )
                except Exception as e:  # noqa: BLE001 -- progress is best-effort
                    logger.warning("Failed to send docling progress update: %s", e)

    async def health_check(self) -> bool:
        return await health(self.api_url)
