"""Client for the embedding gateway's async **batch OCR** routes (Deck #332).

The gateway exposes two batch routes alongside the synchronous ``POST /v1/ocr``
(astrolabe-cloud-website#372):

- ``POST /v1/ocr/batch`` — submit N documents (each with a caller ``custom_id``)
  as one Mistral Batch job; returns ``202`` + a namespaced ``job_id``
  (``<provider>/<batch_job_id>``).
- ``GET /v1/ocr/batch/{job_id}`` — poll; returns the lifecycle status and, once
  terminal, per-document results (per-page markdown, or a per-document error).

The gateway is a **stateless passthrough** to Mistral's Batch API — the
``job_id`` is the only handle, so the worker persists it (see
``vector/batch_ocr_store``) and re-polls across procrastinate retries.

This client submits exactly **one document per job** (the v1 unit; coalescing N
docs/job is a follow-up). Auth + ``/v1`` base-url handling mirror the synchronous
:class:`~nextcloud_mcp_server.embedding.gateway_client.GatewayProvider` /
``_GatewayOcrBackend`` — same M2M :class:`GatewayTokenProvider` bearer, no
provider keys in the pod.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .gateway_client import GatewayTokenProvider

logger = logging.getLogger(__name__)

# Connect timeout for the (cheap) submit/poll calls. These are control-plane-ish
# requests — a submit returns immediately with a job id and a poll is a status
# read — so they get a short, fixed timeout, NOT the document-OCR read timeout
# (which sizes a synchronous transcription).
_BATCH_CONNECT_TIMEOUT_SECONDS = 5.0
_BATCH_REQUEST_TIMEOUT_SECONDS = 30.0

# Gateway-normalised batch lifecycle (OcrBatchStatus on the gateway side).
_PENDING = "pending"
_SUCCEEDED = "succeeded"
_FAILED = "failed"


@dataclass(frozen=True)
class BatchPollResult:
    """One poll of a batch OCR job.

    ``status`` is the gateway-normalised lifecycle (``pending`` | ``succeeded`` |
    ``failed``). For a single-document job: on ``succeeded`` ``pages`` holds the
    document's per-page ``(index, markdown)`` (empty + ``error`` set if that one
    document errored inside an otherwise-successful job); on ``failed`` ``error``
    carries the job-level failure.
    """

    status: str
    # Per-page ``(index, markdown, blocks)`` — ``blocks`` carries surya's per-block
    # layout (normalized [0,1] bbox) when the backend provides it, ``None`` for
    # markdown-only backends (Mistral). Threaded straight into ``_pages_to_text``,
    # which turns blocks into per-block char spans.
    pages: list[tuple[int, str, list[dict[str, Any]] | None]]
    error: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == _PENDING

    @property
    def is_succeeded(self) -> bool:
        return self.status == _SUCCEEDED

    @property
    def is_failed(self) -> bool:
        return self.status == _FAILED


class GatewayBatchOcrClient:
    """Submits + polls single-document batch OCR jobs against the gateway."""

    def __init__(
        self,
        base_url: str,
        model: str,
        token_provider: GatewayTokenProvider | None = None,
    ) -> None:
        # EMBEDDING_GATEWAY_URL is a bare origin; the batch routes live under /v1
        # like the rest of the gateway API. Idempotent if already /v1-suffixed.
        base = base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        self._base = base
        self._model = model
        self._token_provider = token_provider

    async def _headers(self) -> dict[str, str]:
        if self._token_provider is None:
            return {}
        return {"Authorization": f"Bearer {await self._token_provider.get_token()}"}

    async def submit(self, content: bytes, mime_type: str, custom_id: str) -> str:
        """Submit ``content`` as a one-document batch job; return the namespaced
        ``job_id`` to persist + poll. Raises on transport / non-2xx."""
        payload = {
            "model": self._model,
            "documents": [
                {
                    "custom_id": custom_id,
                    "mime_type": mime_type,
                    "document_b64": base64.b64encode(content).decode("ascii"),
                }
            ],
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                _BATCH_REQUEST_TIMEOUT_SECONDS, connect=_BATCH_CONNECT_TIMEOUT_SECONDS
            )
        ) as client:
            resp = await client.post(
                f"{self._base}/ocr/batch", json=payload, headers=await self._headers()
            )
            resp.raise_for_status()
            body = resp.json()
        job_id = body.get("job_id")
        if not job_id:
            # Contract violation (2xx without a job id) — fail with an actionable
            # message rather than a bare KeyError deep in the caller.
            raise ValueError(f"gateway batch submit returned no job_id: {body!r}")
        logger.info(
            "batch OCR submitted: job_id=%s custom_id=%s status=%s",
            job_id,
            custom_id,
            body.get("status"),
        )
        return job_id

    async def poll(self, job_id: str) -> BatchPollResult:
        """Poll a batch job. Raises on transport / non-2xx; maps a terminal job's
        single-document result into :class:`BatchPollResult`.

        ``job_id`` is the gateway's namespaced id (``<provider>/<batch_job_id>``),
        so it embeds a ``/`` and the request path is multi-segment
        (``/v1/ocr/batch/mistral/job-1``). The gateway declares this route with a
        path-capture parameter (``GET /v1/ocr/batch/{job_id:path}``) so the slash
        is captured whole — a plain single-segment ``{job_id}`` would 404 here.
        """
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                _BATCH_REQUEST_TIMEOUT_SECONDS, connect=_BATCH_CONNECT_TIMEOUT_SECONDS
            )
        ) as client:
            resp = await client.get(
                f"{self._base}/ocr/batch/{job_id}", headers=await self._headers()
            )
            resp.raise_for_status()
            body = resp.json()
        status = body.get("status")
        if status is None:
            # A well-formed gateway response always carries status. A 2xx without
            # it is a contract violation: fail fast rather than silently treating
            # it as pending and re-polling until the deadline.
            logger.warning("gateway batch poll returned no status: %r", body)
            return BatchPollResult(
                status=_FAILED, pages=[], error="gateway returned no status"
            )
        if status != _SUCCEEDED:
            # pending: nothing to read yet. failed: surface the job-level error.
            return BatchPollResult(status=status, pages=[], error=body.get("error"))
        return _result_from_success(body)


def _result_from_success(body: dict[str, Any]) -> BatchPollResult:
    """Extract the single document's pages from a succeeded job's results.

    Submitting one document per job means exactly one result item; defensively
    take the first. A per-document error inside a succeeded job (the document
    failed but the job didn't) surfaces as a failed poll so the caller marks the
    doc parse-failed rather than indexing empty text.
    """
    results = body.get("results") or []
    if not results:
        return BatchPollResult(
            status=_FAILED,
            pages=[],
            error="batch job succeeded but returned no results",
        )
    item = results[0]
    # ``not item.get("pages")`` catches both a missing key AND an empty list:
    # a succeeded job that produced zero pages is a per-document failure (nothing
    # to index), not a silent 0-chunk success.
    if item.get("error") is not None or not item.get("pages"):
        return BatchPollResult(
            status=_FAILED, pages=[], error=item.get("error") or "no pages returned"
        )
    # Defensive on both fields (the page index falls back to position) so a
    # malformed page object degrades rather than raising KeyError mid-parse.
    # ``blocks`` (surya layout + normalized bbox) is carried through when present;
    # ``None`` for markdown-only backends.
    pages = [
        (p.get("index", i), p.get("markdown", ""), p.get("blocks"))
        for i, p in enumerate(item["pages"])
    ]
    return BatchPollResult(status=_SUCCEEDED, pages=pages)
