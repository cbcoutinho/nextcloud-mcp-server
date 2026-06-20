"""OCR tier processor.

Routes scanned / no-text-layer PDFs (the tier-0 classifier's ``ocr`` verdict) to
an OCR backend that returns per-page markdown. The single ``ocr`` tier is fully
configurable: operators enable OCR, pick the backend, the model, and sync/batch.

Two interchangeable backends, selected by ``document_ocr_provider``:

  * ``gateway`` -- POST the document to the Astrolabe Cloud model gateway's
    ``POST /v1/ocr`` (the same M2M-authenticated gateway as embeddings; NO
    provider keys in the pod). The gateway routes on the ``<provider>/`` model
    prefix, so one backend serves Mistral, surya, etc. The platform default.
  * ``mistral`` -- call the Mistral OCR API directly from the pod
    (``MISTRAL_API_KEY``), for self-hosters / deployments without the gateway.

``auto`` prefers the gateway (if ``EMBEDDING_GATEWAY_URL`` is set) then direct
Mistral (if ``MISTRAL_API_KEY``). Both return GitHub-flavoured markdown + exact
``page_boundaries``; bbox is re-derived from the PDF bytes + boundaries by
``search/pdf_highlighter``, as for the other tiers.

Batch mode (``document_ocr_mode=batch``) always routes through the embedding
gateway's async Batch OCR job — the gateway is the batching layer, so it works
even when the chosen sync backend is direct Mistral (or, in principle, any
backend that lacks a native batch API). With no gateway configured there is no
batch path from the pod, and ``Settings`` rejects ``mode=batch`` at startup.
"""

import base64
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import anyio
import httpx

from nextcloud_mcp_server.config import Settings, get_settings

from .base import DocumentProcessor, ProcessingResult, ProcessorError

if TYPE_CHECKING:
    # Annotation-only import (the runtime import is lazy, inside
    # build_gateway_batch_client, to avoid a document_processors -> embedding
    # cycle at load).
    from ..embedding.gateway_batch_client import GatewayBatchOcrClient

logger = logging.getLogger(__name__)

# Connect timeout for the OCR backend request. The overall (read) timeout is
# configurable via DOCUMENT_OCR_TIMEOUT_SECONDS and resolved per call.
_OCR_CONNECT_TIMEOUT_SECONDS = 10.0

# Sentinel keys on a ProcessingResult.metadata that mark "batch OCR job still in
# flight — poll again later". The processor can't raise across the registry, so
# it returns this sentinel and ``vector/processor._parse_pdf_tier`` translates it
# into a ``BatchPending`` control-flow raise (same site as ``EscalateError``).
OCR_BATCH_PENDING_KEY = "ocr_batch_pending"
OCR_BATCH_RETRY_IN_KEY = "ocr_batch_retry_in"


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


def _batch_identity(
    options: dict[str, Any] | None,
) -> tuple[str, str, str, str] | None:
    """Extract ``(user_id, doc_id, doc_type, etag)`` from the processor options
    the per-tier path threads in, or ``None`` if identity is absent (the inline
    pool, which can't defer a poll). ``etag`` may be empty (a file with no etag is
    still one tracked job keyed on "").
    """
    if not options:
        return None
    user_id = options.get("user_id")
    doc_id = options.get("doc_id")
    doc_type = options.get("doc_type")
    if not user_id or not doc_id or not doc_type:
        return None
    return str(user_id), str(doc_id), str(doc_type), str(options.get("etag") or "")


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


def _build_gateway_token_provider(settings: Settings) -> Any:
    """Build the M2M ``GatewayTokenProvider`` from settings, or ``None`` when no
    client-id is configured (unauthenticated gateway). Shared by the sync OCR
    backend and the batch client so the M2M-triple validation lives in one place.
    """
    if not settings.embedding_gateway_client_id:
        return None
    # Lazy import avoids a document_processors -> embedding cycle at load.
    from ..embedding.gateway_client import GatewayTokenProvider  # noqa: PLC0415

    # Explicit (not assert -- assert is stripped under `python -O`): the M2M
    # triple is all-or-nothing.
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
    return GatewayTokenProvider(
        token_url=settings.embedding_gateway_token_url,
        client_id=settings.embedding_gateway_client_id,
        client_secret=settings.embedding_gateway_client_secret,
        scope=settings.embedding_gateway_scope,
    )


def build_gateway_batch_client(
    settings: Settings, *, model: str | None = None
) -> "GatewayBatchOcrClient | None":
    """Build a ``GatewayBatchOcrClient`` for batch OCR, or ``None`` when batch is
    not possible (OCR disabled, or no gateway configured).

    The embedding gateway is the batching layer: batch OCR is always reached
    *through* the gateway's ``/v1/ocr/batch`` routes (the model's namespaced prefix
    picks the upstream — Mistral's Batch API, surya, etc.). So batch works even
    when the chosen *sync* backend is direct Mistral — we leverage the gateway to
    batch for backends that have no native batch path from the pod. With no
    ``EMBEDDING_GATEWAY_URL`` there is no batch path, and ``Settings`` rejects
    ``document_ocr_mode=batch`` at startup; this returns ``None`` defensively.

    ``model`` overrides ``settings.document_ocr_model``."""
    if settings.document_ocr_provider == "none":
        return None
    if not settings.embedding_gateway_url:
        return None
    from ..embedding.gateway_batch_client import GatewayBatchOcrClient  # noqa: PLC0415

    return GatewayBatchOcrClient(
        settings.embedding_gateway_url,
        model or settings.document_ocr_model,
        _build_gateway_token_provider(settings),
    )


def build_ocr_backend(
    settings: Settings, *, model: str | None = None
) -> _OcrBackend | None:
    """Select the synchronous OCR backend from settings, or None when none is
    available.

    ``model`` overrides ``settings.document_ocr_model`` (rarely needed; the OCR
    tier binds its own provider-namespaced model id). Backend selection is purely
    provider/model-driven: ``gateway``/``auto`` use the gateway (which routes on the
    model's ``<provider>/`` prefix — Mistral, surya, etc.), ``mistral``/``auto`` use
    the direct Mistral API, and ``none`` disables OCR.
    """
    provider = settings.document_ocr_provider
    # `is not None` (not truthiness): an empty model string must NOT silently fall
    # back to the upstream default.
    model = model if model is not None else settings.document_ocr_model

    if provider == "none":
        return None

    if provider in ("gateway", "auto") and settings.embedding_gateway_url:
        return _GatewayOcrBackend(
            settings.embedding_gateway_url,
            model,
            _build_gateway_token_provider(settings),
        )

    if provider in ("mistral", "auto") and settings.mistral_api_key:
        return _MistralOcrBackend(
            settings.mistral_api_key,
            model,
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
    """The single configurable OCR tier. Backend (gateway vs direct Mistral),
    model, and sync/batch are all chosen from settings; one registered instance
    serves the ``ocr`` tier."""

    def __init__(
        self,
        # The defaults describe the ``ocr`` tier; the keyword args exist for
        # test/bare-construction convenience. Application wiring
        # (document_processors/__init__.py) passes them explicitly.
        *,
        name: str = "ocr",
        tier: str = "ocr",
        model_setting: str = "document_ocr_model",
    ) -> None:
        # The instance is bound to a tier + the settings attribute holding its
        # provider-namespaced model id. Fail fast on a misconfigured model_setting
        # (a typo in a constructor call) so it surfaces at startup, not as an
        # AttributeError mid-OCR. The string only ever comes from hardcoded defaults
        # in __init__.py, never user input.
        if not hasattr(Settings, model_setting):
            raise ValueError(f"Unknown model_setting: {model_setting!r}")
        self._name = name
        self._tier = tier
        self._model_setting = model_setting
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
        # Batch-mode (Deck #332): the gateway batch client is cached like the sync
        # backend so its GatewayTokenProvider keeps its M2M-token cache across
        # documents.
        self._batch_client_resolved = False
        self._batch_client: GatewayBatchOcrClient | None = None
        self._batch_client_lock: anyio.Lock | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> str:
        return self._tier

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

        # Batch mode (Deck #332): submit to the gateway's async Batch OCR job and
        # poll across procrastinate retries. ``_process_batch`` always handles the
        # document — it returns a result (incl. the "pending" sentinel) or RAISES.
        # It never silently downgrades to synchronous OCR: if no batch-capable
        # backend is configured the operator opted into a mode that can't run, and
        # the failure is surfaced (``Settings`` also rejects this at startup) rather
        # than quietly transcribing synchronously.
        #
        # A transport error from _process_batch (e.g. the gateway briefly down)
        # is intentionally NOT caught here: it propagates to procrastinate for a
        # durable retry.
        if settings.document_ocr_mode == "batch":
            return await self._process_batch(
                content, content_type, filename, options, settings
            )

        if not self._backend_resolved:
            if self._backend_lock is None:
                self._backend_lock = anyio.Lock()
            async with self._backend_lock:
                if not self._backend_resolved:  # double-checked
                    self._backend = build_ocr_backend(
                        settings,
                        model=getattr(settings, self._model_setting),
                    )
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

    async def _get_batch_client(self) -> "GatewayBatchOcrClient | None":
        """Cached gateway batch client (or ``None`` when batch isn't possible — OCR
        disabled, or no gateway configured). Resolved once under its own lock so the
        token provider's M2M cache survives across documents.

        Batch OCR always routes through the embedding gateway (the batching layer),
        so this is non-``None`` whenever a gateway is configured, regardless of the
        chosen *sync* backend — that is how we batch for direct backends that lack a
        native batch API."""
        if not self._batch_client_resolved:
            if self._batch_client_lock is None:
                self._batch_client_lock = anyio.Lock()
            async with self._batch_client_lock:
                if not self._batch_client_resolved:  # double-checked
                    settings = get_settings()
                    self._batch_client = build_gateway_batch_client(
                        settings,
                        model=getattr(settings, self._model_setting),
                    )
                    self._batch_client_resolved = True
        return self._batch_client

    async def _process_batch(
        self,
        content: bytes,
        content_type: str,
        filename: str | None,
        options: dict[str, Any] | None,
        settings: Settings,
    ) -> ProcessingResult:
        """Submit + poll a one-document batch OCR job.

        Always handles the document in batch mode: returns a terminal
        :class:`ProcessingResult` (success/failure), or the *pending sentinel*
        (``success=False`` + ``OCR_BATCH_PENDING_KEY`` metadata) that
        ``_parse_pdf_tier`` turns into a ``BatchPending`` re-poll. It NEVER falls
        back to synchronous OCR — when batch can't run it raises
        :class:`ProcessorError` rather than silently downgrading (``Settings`` also
        rejects ``mode=batch`` without a gateway at startup).
        """
        client = await self._get_batch_client()
        if client is None:
            # No batch path from the pod: batch OCR routes through the embedding
            # gateway, and either OCR is disabled (provider=none) or no gateway is
            # configured. Fail loud rather than transcribe synchronously.
            raise ProcessorError(
                "DOCUMENT_OCR_MODE=batch requires the embedding gateway: set "
                "EMBEDDING_GATEWAY_URL (and an enabled DOCUMENT_OCR_PROVIDER) so "
                "batch OCR can route through the gateway's async Batch API, or use "
                "DOCUMENT_OCR_MODE=sync."
            )
        # Per-doc identity is threaded via ``options`` only on the per-tier
        # procrastinate path; the inline/memory pool omits it and can't defer a
        # poll, so batch cannot run there.
        identity = _batch_identity(options)
        if identity is None:
            raise ProcessorError(
                "DOCUMENT_OCR_MODE=batch is only supported on the worker (postgres) "
                "ingest path, which defers the batch poll across retries; the "
                "inline/memory pool cannot. Use DOCUMENT_OCR_MODE=sync for in-process "
                "ingestion."
            )

        # Lazy import: keep the vector/DB stack off the document_processors load
        # path (mirrors the EscalateError lazy import in vector/processor).
        from ..vector.batch_ocr_store import BatchOcrJobStore  # noqa: PLC0415

        user_id, doc_id, doc_type, etag = identity
        store = await BatchOcrJobStore.shared()
        mime = content_type.split(";")[0].strip().lower()
        poll_seconds = settings.document_ocr_batch_poll_seconds

        job = await store.get(
            user_id=user_id, doc_id=doc_id, doc_type=doc_type, etag=etag
        )
        if job is None:
            # New submission. Drop any superseded-version rows for this doc first
            # (a re-edited file changes etag) — a no-op on the very first submit,
            # one cheap DELETE on a resubmit. Then submit + record.
            await store.delete_stale_for_doc(
                user_id=user_id, doc_id=doc_id, doc_type=doc_type, keep_etag=etag
            )
            job_id = await client.submit(content, mime, custom_id=doc_id)
            await store.insert_pending(
                user_id=user_id,
                doc_id=doc_id,
                doc_type=doc_type,
                etag=etag,
                job_id=job_id,
            )
            logger.info(
                "batch OCR job submitted for %s (job_id=%s); deferring poll",
                filename or doc_id,
                job_id,
            )
            return self._pending(poll_seconds)

        # Existing job — poll the gateway.
        result = await client.poll(job.job_id)
        if result.is_pending:
            elapsed = int(time.time()) - job.submitted_at
            if elapsed >= settings.document_ocr_batch_max_wait_seconds:
                await store.delete(
                    user_id=user_id, doc_id=doc_id, doc_type=doc_type, etag=etag
                )
                # We don't cancel the gateway-side job (there's no cancel endpoint
                # at this layer) — it keeps running and is reaped by the gateway's
                # own file purge. Dropping the row just stops us polling it.
                logger.warning(
                    "batch OCR job %s exceeded max wait (%ss); marking failed",
                    job.job_id,
                    settings.document_ocr_batch_max_wait_seconds,
                )
                return ProcessingResult(
                    text="",
                    metadata={"parse_failed_reason": "timeout"},
                    processor=self.name,
                    success=False,
                    error="batch OCR timed out",
                )
            return self._pending(poll_seconds)

        # Terminal — drop the tracking row either way.
        await store.delete(user_id=user_id, doc_id=doc_id, doc_type=doc_type, etag=etag)
        if result.is_failed:
            logger.warning(
                "batch OCR job %s failed: %s", job.job_id, result.error or "unknown"
            )
            return ProcessingResult(
                text="",
                metadata={"parse_failed_reason": "error"},
                processor=self.name,
                success=False,
                error=f"batch OCR failed: {result.error or 'unknown'}",
            )
        if not result.is_succeeded:
            # Defensive: poll() maps anything that isn't "succeeded" to its raw
            # status, and only pending/succeeded/failed are handled above. An
            # unexpected terminal status (gateway version skew, a new lifecycle
            # state) must NOT fall through to _pages_to_text([]) -> a 0-chunk
            # "success" that silently indexes empty text and re-submits forever.
            logger.warning(
                "batch OCR job %s returned unexpected status %r; marking failed",
                job.job_id,
                result.status,
            )
            return ProcessingResult(
                text="",
                metadata={"parse_failed_reason": "error"},
                processor=self.name,
                success=False,
                error=f"unexpected batch status: {result.status}",
            )
        text, boundaries = _pages_to_text(result.pages)
        return ProcessingResult(
            text=text,
            metadata={
                "page_count": len(boundaries),
                "page_boundaries": boundaries,
                "file_size": len(content),
            },
            processor=self.name,
        )

    def _pending(self, retry_in: int) -> ProcessingResult:
        """The pending sentinel — ``_parse_pdf_tier`` raises ``BatchPending`` from
        it. ``success=False`` keeps it out of the index path, and the sentinel key
        keeps it out of the parse-failed path (it isn't a failure)."""
        return ProcessingResult(
            text="",
            metadata={OCR_BATCH_PENDING_KEY: True, OCR_BATCH_RETRY_IN_KEY: retry_in},
            processor=self.name,
            success=False,
        )

    async def health_check(self) -> bool:
        # Backends are resolved lazily (and configured per tenant), so there is
        # nothing to probe here without making a billable upstream call -- the
        # processor reports healthy and surfaces a real failure per-document.
        return True
