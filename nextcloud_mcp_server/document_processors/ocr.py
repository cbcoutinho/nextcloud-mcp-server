"""Tier-3 OCR processor.

Routes scanned / no-text-layer PDFs (the tier-0 classifier's ``ocr`` verdict) to
an OCR backend that returns per-page markdown. Two interchangeable backends,
selected by ``document_ocr_provider``:

  * ``gateway`` -- POST the document to the Astrolabe Cloud model gateway's
    ``POST /v1/ocr`` (the same M2M-authenticated gateway as embeddings; NO
    provider keys in the pod). The platform default.
  * ``mistral`` -- call the Mistral OCR API directly from the pod
    (``MISTRAL_API_KEY``), for self-hosters / deployments without the gateway.

``auto`` prefers the gateway (if ``EMBEDDING_GATEWAY_URL`` is set) then direct
Mistral (if ``MISTRAL_API_KEY``). Both return GitHub-flavoured markdown + exact
``page_boundaries``; bbox is re-derived from the PDF bytes + boundaries by
``search/pdf_highlighter``, as for the other tiers.
"""

import base64
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import httpx

from nextcloud_mcp_server.config import Settings, get_settings

from .base import DocumentProcessor, ProcessingResult

logger = logging.getLogger(__name__)

# Connect timeout for the OCR backend request. The overall (read) timeout is
# configurable via DOCUMENT_OCR_TIMEOUT_SECONDS and resolved per call.
_OCR_CONNECT_TIMEOUT_SECONDS = 10.0


def _pages_to_text(
    pages: list[tuple[int, str]],
) -> tuple[str, list[dict[str, Any]]]:
    """Join per-page markdown (ordered by index) into one string + boundaries.

    Pages are joined with a blank line. Boundaries are kept CONTIGUOUS (each
    page owns its leading ``\\n\\n`` separator) so they index exactly into the
    returned text and ``boundaries[-1]["end_offset"] == len(text)`` -- the
    ``search/pdf_highlighter`` contract. Consequence: a page's range starts at
    its separator, not its first glyph (the fast pypdfium2 path joins with no
    separator, so its ranges are glyph-tight). The 2-char offset is immaterial
    to page-level chunk attribution.
    """
    sep = "\n\n"
    parts: list[str] = []
    boundaries: list[dict[str, Any]] = []
    offset = 0
    for i, (index, markdown) in enumerate(sorted(pages, key=lambda p: p[0])):
        chunk = markdown if i == 0 else sep + markdown
        start = offset
        offset += len(chunk)
        parts.append(chunk)
        boundaries.append(
            {"page": index + 1, "start_offset": start, "end_offset": offset}
        )
    return "".join(parts), boundaries


class _OcrBackend(ABC):
    @abstractmethod
    async def ocr(
        self, content: bytes, mime_type: str
    ) -> tuple[str, list[dict[str, Any]]]: ...


class _GatewayOcrBackend(_OcrBackend):
    """Calls the model gateway's ``POST /v1/ocr`` (key-isolated, M2M-authed)."""

    def __init__(self, base_url: str, model: str, token_provider: Any = None):
        base = base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        self._url = f"{base}/ocr"
        self._model = model
        self._token_provider = token_provider

    async def ocr(
        self, content: bytes, mime_type: str
    ) -> tuple[str, list[dict[str, Any]]]:
        headers: dict[str, str] = {}
        if self._token_provider is not None:
            headers["Authorization"] = (
                f"Bearer {await self._token_provider.get_token()}"
            )
        payload = {
            "model": self._model,
            "document_b64": base64.b64encode(content).decode("ascii"),
            "mime_type": mime_type,
        }
        # Resolved per call (get_settings builds fresh) so test monkeypatching is
        # honoured; a live tenant change still needs a restart because the backend
        # instance itself is cached for the pod's lifetime.
        ocr_timeout = get_settings().document_ocr_timeout_seconds
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(ocr_timeout, connect=_OCR_CONNECT_TIMEOUT_SECONDS)
        ) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        pages = [(p["index"], p.get("markdown", "")) for p in body.get("pages", [])]
        return _pages_to_text(pages)


class _MistralOcrBackend(_OcrBackend):
    """Calls the Mistral OCR API directly (provider key lives in the pod)."""

    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        from mistralai.client import Mistral  # noqa: PLC0415 -- lazy SDK import

        self._client = Mistral(api_key=api_key, server_url=base_url)
        # The gateway-namespaced "mistral/<model>" id strips down to the bare
        # upstream model the SDK expects.
        self._model = model.split("/", 1)[-1]

    async def ocr(
        self, content: bytes, mime_type: str
    ) -> tuple[str, list[dict[str, Any]]]:
        data_url = (
            f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"
        )
        # Apply DOCUMENT_OCR_TIMEOUT_SECONDS uniformly with the gateway backend.
        # The Mistral SDK manages its own httpx client, so wrap the call in an
        # anyio cancel-scope timeout rather than threading a per-request timeout
        # through the SDK; on expiry this raises TimeoutError, which the
        # OcrProcessor turns into a clean parse failure.
        ocr_timeout = get_settings().document_ocr_timeout_seconds
        with anyio.fail_after(ocr_timeout):
            resp = await self._client.ocr.process_async(
                model=self._model,
                document={"type": "document_url", "document_url": data_url},
            )
        pages = [(p.index, p.markdown or "") for p in (resp.pages or [])]
        return _pages_to_text(pages)


def build_ocr_backend(settings: Settings) -> _OcrBackend | None:
    """Select an OCR backend from settings, or None when none is available."""
    provider = settings.document_ocr_provider
    if provider == "none":
        return None

    if provider in ("gateway", "auto") and settings.embedding_gateway_url:
        token_provider = None
        if settings.embedding_gateway_client_id:
            # Lazy import avoids a document_processors -> embedding cycle at load.
            from ..embedding.gateway_client import (  # noqa: PLC0415
                GatewayTokenProvider,
            )

            # Explicit (not assert -- assert is stripped under `python -O`): the
            # M2M triple is all-or-nothing.
            if not settings.embedding_gateway_token_url:
                raise ValueError(
                    "EMBEDDING_GATEWAY_TOKEN_URL is required when "
                    "EMBEDDING_GATEWAY_CLIENT_ID is set"
                )
            if not settings.embedding_gateway_client_secret:
                raise ValueError(
                    "EMBEDDING_GATEWAY_CLIENT_SECRET is required when "
                    "EMBEDDING_GATEWAY_CLIENT_ID is set"
                )
            token_provider = GatewayTokenProvider(
                token_url=settings.embedding_gateway_token_url,
                client_id=settings.embedding_gateway_client_id,
                client_secret=settings.embedding_gateway_client_secret,
                scope=settings.embedding_gateway_scope,
            )
        return _GatewayOcrBackend(
            settings.embedding_gateway_url, settings.document_ocr_model, token_provider
        )

    if provider in ("mistral", "auto") and settings.mistral_api_key:
        return _MistralOcrBackend(
            settings.mistral_api_key,
            settings.document_ocr_model,
            settings.mistral_base_url,
        )

    # An EXPLICIT provider that's missing its config is an operator error -- warn
    # loudly (once, since the backend is resolved+cached) rather than silently
    # disabling OCR. "auto"/"none" fall through to None quietly by design.
    if provider == "gateway":
        logger.warning(
            "DOCUMENT_OCR_PROVIDER=gateway but EMBEDDING_GATEWAY_URL is unset; "
            "OCR is disabled"
        )
    elif provider == "mistral":
        logger.warning(
            "DOCUMENT_OCR_PROVIDER=mistral but MISTRAL_API_KEY is unset; "
            "OCR is disabled"
        )
    return None


class OcrProcessor(DocumentProcessor):
    """Tier-3 OCR processor (gateway or direct Mistral backend)."""

    def __init__(self) -> None:
        # Resolve the backend once and reuse it: rebuilding per call would create
        # a fresh GatewayTokenProvider each time (discarding its M2M-token cache
        # -> a token fetch per document) and a new Mistral SDK client per call.
        # A config change needs a pod restart anyway, so caching for the pod's
        # lifetime is safe.
        self._backend_resolved = False
        self._backend: _OcrBackend | None = None
        # Serialise first-call resolution so a burst of concurrent OCR requests
        # doesn't each build a backend (and fetch its own M2M token). Lazy-init:
        # anyio primitives must not be created at import time.
        self._backend_lock: anyio.Lock | None = None

    @property
    def name(self) -> str:
        return "ocr"

    @property
    def tier(self) -> str:
        return "ocr"

    @property
    def supported_mime_types(self) -> set[str]:
        return {"application/pdf"}

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
        settings = get_settings()
        if not self._backend_resolved:
            if self._backend_lock is None:
                self._backend_lock = anyio.Lock()
            async with self._backend_lock:
                if not self._backend_resolved:  # double-checked
                    self._backend = build_ocr_backend(settings)
                    self._backend_resolved = True
        backend = self._backend
        if backend is None:
            logger.warning(
                "OCR requested for %s but no backend is configured (provider=%s)",
                filename or "<bytes>",
                settings.document_ocr_provider,
            )
            return ProcessingResult(
                text="",
                metadata={"parse_failed_reason": "unsupported"},
                processor=self.name,
                success=False,
                error="no OCR backend configured",
            )
        try:
            text, boundaries = await backend.ocr(
                content, content_type.split(";")[0].strip().lower()
            )
        except (TimeoutError, httpx.TimeoutException):
            # Two timeout shapes reach here: the Mistral backend's
            # anyio.fail_after raises the builtin TimeoutError, while the gateway
            # backend's httpx.Timeout raises httpx.ReadTimeout (a
            # httpx.TimeoutException, NOT a TimeoutError). Catch both so a
            # too-low DOCUMENT_OCR_TIMEOUT_SECONDS lands in its own reason bucket
            # rather than being conflated with provider errors.
            timeout = settings.document_ocr_timeout_seconds
            logger.warning(
                "OCR timed out for %s after %.1fs", filename or "<bytes>", timeout
            )
            return ProcessingResult(
                text="",
                metadata={"parse_failed_reason": "timeout"},
                processor=self.name,
                success=False,
                error=f"OCR timed out after {timeout:.1f}s",
            )
        except Exception as e:
            logger.warning("OCR failed for %s: %s", filename or "<bytes>", e)
            return ProcessingResult(
                text="",
                metadata={"parse_failed_reason": "error"},
                processor=self.name,
                success=False,
                error=f"{type(e).__name__}: {e}",
            )
        return ProcessingResult(
            text=text,
            metadata={
                "page_count": len(boundaries),
                "page_boundaries": boundaries,
                "file_size": len(content),
            },
            processor=self.name,
        )

    async def health_check(self) -> bool:
        # Backends are resolved lazily (and configured per tenant), so there is
        # nothing to probe here without making a billable upstream call -- the
        # processor reports healthy and surfaces a real failure per-document.
        return True
