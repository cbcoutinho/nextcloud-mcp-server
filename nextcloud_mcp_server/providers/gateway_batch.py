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
:class:`~nextcloud_mcp_server.providers.gateway.GatewayProvider` /
``_GatewayOcrBackend`` — same M2M :class:`GatewayTokenProvider` bearer, no
provider keys in the pod.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .gateway import GatewayTokenProvider

logger = logging.getLogger(__name__)

# Connect timeout for the submit/poll calls. Neither waits on OCR itself, so
# neither gets the document-OCR read timeout (which sizes a synchronous
# transcription).
_BATCH_CONNECT_TIMEOUT_SECONDS = 5.0
# A poll IS control-plane-ish: a small status read that returns immediately.
_BATCH_REQUEST_TIMEOUT_SECONDS = 30.0
# A submit is NOT: it uploads the whole document, base64-inflated by 4/3, and the
# gateway stages those bytes to object storage before it answers. 30s is a
# multi-MB-PDF-sized cliff, and losing the race is worse than a slow call — the
# gateway has already created the job, so a client-side timeout strands it: the
# caller never learns the job_id, `insert_pending` never runs, and the retry
# submits a SECOND job for the same document, paying for the OCR twice (Deck
# #1084: 16 of 339 submissions in one 12h window). Size it for the upload.
_BATCH_SUBMIT_TIMEOUT_SECONDS = 180.0

# Gateway-normalised batch lifecycle (OcrBatchStatus on the gateway side).
_PENDING = "pending"
_SUCCEEDED = "succeeded"
_FAILED = "failed"


class OcrBatchJobNotFound(Exception):
    """The gateway returned ``404`` for a poll — it has no record of this batch
    job. Its durable row was purged (retention) or lost/orphaned (e.g. a gateway
    pod move mid-flight). Distinct from a transport/5xx error: the caller must
    DROP the persisted job id and re-submit a fresh job, not keep polling a dead
    id forever (incident 2026-07-03 — one doc polled a purged id from 2026-07-01
    for ~2.5 days, flapping the burst GPU)."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"gateway has no record of batch OCR job {job_id!r}")
        self.job_id = job_id


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
    # Seconds the gateway asked us to wait before polling again (its Retry-After on
    # a pending 202). None if absent/unparseable; the caller falls back to its own
    # configured poll interval and applies a floor. Deck #523.
    retry_after: int | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == _PENDING

    @property
    def is_succeeded(self) -> bool:
        return self.status == _SUCCEEDED

    @property
    def is_failed(self) -> bool:
        return self.status == _FAILED


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a Retry-After header as delta-seconds. The gateway sends an integer
    number of seconds (never an HTTP-date), so a non-integer / non-positive / absent
    value yields None and the caller falls back to its configured poll interval."""
    if value is None:
        return None
    try:
        seconds = int(value.strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


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
                _BATCH_SUBMIT_TIMEOUT_SECONDS, connect=_BATCH_CONNECT_TIMEOUT_SECONDS
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
        """Poll a batch job. Maps a terminal job's single-document result into
        :class:`BatchPollResult`. A ``404`` (the gateway has no record of this job —
        row purged/orphaned) raises :class:`OcrBatchJobNotFound` so the caller can
        re-submit. A transport error or a 5xx yields a PENDING result (the job was
        accepted; "no answer" is not "no job" — Deck #1192); any other non-2xx
        propagates as a hard failure.

        ``job_id`` is the gateway's namespaced id (``<provider>/<batch_job_id>``),
        so it embeds a ``/`` and the request path is multi-segment
        (``/v1/ocr/batch/mistral/job-1``). The gateway declares this route with a
        path-capture parameter (``GET /v1/ocr/batch/{job_id:path}``) so the slash
        is captured whole — a plain single-segment ``{job_id}`` would 404 here.
        """
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    _BATCH_REQUEST_TIMEOUT_SECONDS,
                    connect=_BATCH_CONNECT_TIMEOUT_SECONDS,
                )
            ) as client:
                resp = await client.get(
                    f"{self._base}/ocr/batch/{job_id}", headers=await self._headers()
                )
                if resp.status_code == 404:
                    # The gateway has no record of this job (a store-backed provider
                    # 404s an unknown id). Raise a typed error so the caller re-submits
                    # instead of treating it as a generic failure and re-polling the
                    # dead id forever.
                    raise OcrBatchJobNotFound(job_id)
                resp.raise_for_status()
                body = resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            # No usable answer (read/connect timeout, or a gateway 5xx — e.g. its
            # upstream provider leg failing). Indistinguishable from "still working":
            # the job WAS accepted, so the gateway owns its lifecycle (Deck #523) and
            # PENDING is the only safe reading.
            #
            # Deck #1192: this used to propagate, where TieredEscalationStrategy
            # counted it as a transient infra error against ``max_transient_attempts``
            # and, once that was spent, TERMINALLY DROPPED a document the gateway may
            # still have been OCRing — silent data loss (~200 docs / 10 min at peak).
            #
            # 429 joins the 5xx/transport set: "poll slower" is a statement about US,
            # never about the job, so hard-failing it would reintroduce that exact
            # drop for a gateway that rate-limits polls under load. Any OTHER 4xx is a
            # real client-side fault and still propagates.
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code < 500
                and exc.response.status_code != httpx.codes.TOO_MANY_REQUESTS
            ):
                raise
            # A 429 (or any error response) may carry Retry-After; honour it so a
            # rate-limited poll backs off as asked instead of at our own interval.
            retry_after = (
                _parse_retry_after(exc.response.headers.get("Retry-After"))
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            logger.warning(
                "batch OCR poll got no answer for job %s (%s: %s); treating as "
                "pending and re-polling",
                job_id,
                type(exc).__name__,
                exc,
            )
            return BatchPollResult(status=_PENDING, pages=[], retry_after=retry_after)
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
            # pending: nothing to read yet — honour the gateway's Retry-After so a
            # large pending backlog doesn't storm it (Deck #523). failed: surface
            # the job-level error. resp.headers stays valid after the client closes
            # (the response is fully read).
            return BatchPollResult(
                status=status,
                pages=[],
                error=body.get("error"),
                retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
            )
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
