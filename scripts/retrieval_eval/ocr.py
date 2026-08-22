"""OCR tier for the eval harness — fills the born-digital blind spot.

Some OHR-Bench gold docs are scanned/image-only (no text layer), so the tier-1
extractor (`pypdfium2_fast._extract`) returns empty and they never get indexed —
their questions auto-miss. This module OCRs those docs through the gateway and
reconstructs the same ``(full_text, page_boundaries)`` contract the chunkers
consume, so the OCR text flows through the identical chunk -> embed -> index path.

Two engines (per the gateway):
- **surya/surya-ocr-2** — self-hosted, in-cluster. Submitted via the BATCH API
  (`POST /v1/ocr/batch` + poll `GET /v1/ocr/batch/{job_id}`); the batch submit
  triggers leaf.cloud GPU provisioning, so poll patiently to completion.
- **mistral/mistral-ocr-*** — cloud API. Uses the SYNC endpoint (`POST /v1/ocr`),
  one document per call; no GPU spin-up.

Both return per-page ``markdown``; we join with no separator (offsets stay exact,
matching `_extract`'s contract) and build ``page_boundaries``.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import anyio
import httpx

from .clients import _post_json

logger = logging.getLogger(__name__)


def pages_to_extract(pages_markdown: list[str]) -> tuple[str, dict[str, Any]]:
    """Build the ``_extract`` contract from per-page OCR markdown.

    ``full_text`` = pages joined with NO separator so ``page_boundaries`` offsets
    index it exactly (same invariant as `pypdfium2_fast._extract`).
    """
    boundaries: list[dict[str, Any]] = []
    offset = 0
    for n, md in enumerate(pages_markdown, start=1):
        boundaries.append(
            {"page": n, "start_offset": offset, "end_offset": offset + len(md)}
        )
        offset += len(md)
    full_text = "".join(pages_markdown)
    return full_text, {"page_count": len(pages_markdown), "page_boundaries": boundaries}


def _pages_markdown(pages: list[dict] | None) -> list[str]:
    """Per-page markdown, ordered by the page ``index`` (gateway may reorder)."""
    ordered = sorted(pages or [], key=lambda pg: pg.get("index", 0))
    return [pg.get("markdown", "") or "" for pg in ordered]


class MistralSyncOCR:
    """Cloud OCR via the sync ``POST /v1/ocr`` — one PDF per call, no GPU."""

    def __init__(
        self, client: httpx.AsyncClient, model: str, limiter: anyio.CapacityLimiter
    ):
        self._client = client
        self._model = model
        self._limiter = limiter
        self.label = model

    async def ocr_doc(self, pdf_bytes: bytes) -> list[str]:
        b64 = base64.b64encode(pdf_bytes).decode()
        async with self._limiter:
            data = await _post_json(
                self._client,
                "/v1/ocr",
                {
                    "model": self._model,
                    "document_b64": b64,
                    "mime_type": "application/pdf",
                },
            )
        # Sync response: {pages: [{index, markdown, blocks?}]} (production shape).
        pages = data.get("pages")
        if pages is None and isinstance(data.get("result"), dict):
            pages = data["result"].get("pages")
        return _pages_markdown(pages)


class SuryaBatchOCR:
    """Self-hosted OCR via the BATCH API — triggers the leaf.cloud GPU.

    Submits all docs in one batch job, then polls to completion. The GPU is off
    at submit time; provisioning takes minutes, so use a generous poll budget.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        model: str,
        *,
        poll_seconds: float = 10.0,
        max_wait_seconds: float = 3600.0,
    ):
        self._client = client
        self._model = model
        self._poll = poll_seconds
        self._max_wait = max_wait_seconds
        self.label = model

    async def ocr_docs(
        self, docs: list[tuple[str, bytes]], *, reuse_job_id: str | None = None
    ) -> dict[str, list[str]]:
        """OCR many docs in one batch job. Returns ``{doc_key: [page_markdown]}``.

        ``docs`` = ``[(doc_key, pdf_bytes), ...]``. custom_id = doc_key. Completed
        job results stay re-fetchable, so ``reuse_job_id`` skips submission (and
        the GPU trigger) and re-polls an existing job — used to recover after a
        downstream crash without paying for OCR again.
        """
        if reuse_job_id:
            job_id = reuse_job_id
            logger.info("surya batch %s REUSED (no re-submit, no GPU)", job_id)
        else:
            documents = [
                {
                    "custom_id": doc_key,
                    "mime_type": "application/pdf",
                    "document_b64": base64.b64encode(pdf_bytes).decode(),
                }
                for doc_key, pdf_bytes in docs
            ]
            submit = await _post_json(
                self._client,
                "/v1/ocr/batch",
                {"model": self._model, "documents": documents},
            )
            job_id = submit["job_id"]
            logger.info(
                "surya batch %s submitted (%d docs) — GPU provisioning, polling...",
                job_id,
                len(documents),
            )
        waited = 0.0
        terminal = {"succeeded", "failed", "completed", "error"}
        while waited < self._max_wait:
            await anyio.sleep(self._poll)
            waited += self._poll
            resp = await self._client.get(f"/v1/ocr/batch/{job_id}", timeout=60)
            resp.raise_for_status()
            job = resp.json()
            status = job.get("status")
            if status in terminal:
                logger.info(
                    "surya batch %s -> %s (%s/%s docs) after %.0fs",
                    job_id,
                    status,
                    job.get("succeeded"),
                    job.get("total"),
                    waited,
                )
                out: dict[str, list[str]] = {}
                for item in job.get("results") or []:
                    if item.get("error"):
                        logger.warning(
                            "surya ocr error %s: %s",
                            item.get("custom_id"),
                            item["error"],
                        )
                        continue
                    out[item["custom_id"]] = _pages_markdown(item.get("pages"))
                return out
            logger.info("surya batch %s status=%s (%.0fs)", job_id, status, waited)
        raise TimeoutError(f"surya batch {job_id} did not finish in {self._max_wait}s")
