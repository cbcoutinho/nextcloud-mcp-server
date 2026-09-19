"""Legacy and ODF office documents, converted by a shared Collabora Online service.

``.doc``/``.xls``/``.ppt`` (OLE2) and ``.odt``/``.ods``/``.odp`` (ODF) have no
pure-Python reader worth using, and LibreOffice is the one thing that opens them
all. Rather than bake a LibreOffice binary into this image (#1265), the file is
sent to Collabora Online's stateless ``POST /cool/convert-to/<format>`` endpoint
(coolwsd) -- a service many Nextcloud deployments already run as Nextcloud
Office, which any number of consumers can share (ADR-039).

The conversion is container-to-container, never to PDF: each format becomes its
OOXML counterpart and is handed to the native reader for that format (ADR-036/
038), so tables, headings, speaker notes and picture captioning all behave
exactly as they do for a file that arrived as OOXML.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Optional

import httpx

from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .presentation import PPTX_MIME
from .spreadsheet import XLSX_MIME
from .word import DOCX_MIME

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"

# Source MIME -> (extension coolwsd picks its import filter by, OOXML target,
# the container signature the bytes must start with).
CONVERSIONS: dict[str, tuple[str, str, bytes]] = {
    "application/msword": ("doc", "docx", _OLE2_MAGIC),
    "application/vnd.ms-excel": ("xls", "xlsx", _OLE2_MAGIC),
    "application/vnd.ms-powerpoint": ("ppt", "pptx", _OLE2_MAGIC),
    "application/vnd.oasis.opendocument.text": ("odt", "docx", _ZIP_MAGIC),
    "application/vnd.oasis.opendocument.spreadsheet": ("ods", "xlsx", _ZIP_MAGIC),
    "application/vnd.oasis.opendocument.presentation": ("odp", "pptx", _ZIP_MAGIC),
}

_OOXML_MIME = {"docx": DOCX_MIME, "xlsx": XLSX_MIME, "pptx": PPTX_MIME}

_CONNECT_TIMEOUT_SECONDS = 10.0


async def convert(
    api_url: str, content: bytes, source_ext: str, target: str, timeout: float
) -> bytes:
    """Convert ``content`` to ``target`` with coolwsd and return the bytes.

    coolwsd answers 400 for a missing format or file part, 403 for a client
    outside its ``net.post_allow`` list, and 500 with an empty body for a file
    its import filter rejects -- all of them raise :class:`ProcessorError`.
    """
    url = f"{api_url.rstrip('/')}/cool/convert-to/{target}"
    # The part's filename matters: its extension selects the import filter.
    files = {"data": (f"document.{source_ext}", content)}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=_CONNECT_TIMEOUT_SECONDS)
        ) as client:
            response = await client.post(url, files=files)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        hint = (
            " (is this server in coolwsd's net.post_allow list?)"
            if exc.response.status_code == 403
            else ""
        )
        raise ProcessorError(
            f"Collabora conversion to {target} failed: HTTP "
            f"{exc.response.status_code}{hint}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ProcessorError(f"Collabora conversion to {target} failed: {exc}") from exc
    if not response.content:
        raise ProcessorError(f"Collabora returned an empty {target}")
    return response.content


class CollaboraProcessor(DocumentProcessor):
    """Convert legacy/ODF office files to OOXML, then read them natively."""

    def __init__(
        self,
        api_url: str,
        readers: Mapping[str, DocumentProcessor],
        timeout: float = 60.0,
    ) -> None:
        """``readers`` maps an OOXML extension (``docx``/``xlsx``/``pptx``) to
        the processor that reads it."""
        self._api_url = api_url
        self._readers = readers
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "collabora"

    @property
    def tier(self) -> str:
        return "fast"

    @property
    def supported_mime_types(self) -> set[str]:
        return set(CONVERSIONS)

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
        base_type = content_type.split(";")[0].strip().lower()
        source_ext, target, magic = CONVERSIONS[base_type]
        # LibreOffice falls back to a plain-text import for bytes it does not
        # recognise, so coolwsd happily "converts" garbage into a document of
        # garbage. Refuse anything that is not the container the type claims.
        if not content.startswith(magic):
            raise ProcessorError(
                f"{filename or 'file'} is not a {source_ext} container "
                f"(content does not match {base_type})"
            )

        if progress_callback:
            await progress_callback(0.0, None, f"Converting .{source_ext} to .{target}")
        converted = await convert(
            self._api_url, content, source_ext, target, self._timeout
        )

        reader = self._readers[target]
        result = await reader.process(
            converted, _OOXML_MIME[target], filename, options, progress_callback
        )
        result.metadata["converted_from_mime"] = base_type
        result.metadata["converted_by"] = self.name
        result.processor = f"{self.name}+{result.processor}"
        return result

    async def health_check(self) -> bool:
        """coolwsd advertises whether this client may use convert-to."""
        url = f"{self._api_url.rstrip('/')}/hosting/capabilities"
        try:
            async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT_SECONDS) as client:
                response = await client.get(url)
                response.raise_for_status()
                return bool(response.json().get("convert-to", {}).get("available"))
        except (httpx.HTTPError, ValueError, AttributeError):
            return False
