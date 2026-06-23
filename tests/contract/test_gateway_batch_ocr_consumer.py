"""Consumer contract: nextcloud-mcp-server -> embedding-gateway batch OCR (Deck #332).

When ``DOCUMENT_OCR_MODE=batch`` the ingest worker drives the gateway's async
Batch OCR routes via :class:`GatewayBatchOcrClient`
(``embedding/gateway_batch_client.py``):

- ``POST /v1/ocr/batch`` — submit one document, returns a namespaced ``job_id``.
- ``GET /v1/ocr/batch/{job_id}`` — poll; pending until terminal, then per-page
  markdown (succeeded) or an error (failed).

This pact pins the request/response shapes the consumer depends on, for the
``astrolabe-cloud-gateway`` provider (whose verification job lives in
astrolabe-cloud-website, services/embedding-gateway). Only the fields the client
actually reads are asserted, so the contract stays robust to additive response
changes (the gateway's ``OcrBatchJobOut`` carries more fields — total/completed/
counts — that the single-document client ignores).

The gateway is unauthenticated today, so no bearer is sent (matching the
M2M-optional ``GatewayBatchOcrClient``). See ADR-029 for the contract-testing
architecture.
"""

import base64

import pytest
from pact import match

from nextcloud_mcp_server.embedding.gateway_batch_client import GatewayBatchOcrClient

pytestmark = pytest.mark.contract

_MODEL = "mistral/mistral-ocr-latest"
# A small, valid base64 PDF payload — the gateway base64-decodes + size-checks
# the document, so the replayed request must carry decodable bytes.
_PDF_B64 = base64.b64encode(b"%PDF-1.4 contract test").decode("ascii")


async def test_submit_returns_namespaced_job_id(gateway_consumer_pact):
    (
        gateway_consumer_pact.upon_receiving("a batch OCR submission for one document")
        .given("the gateway accepts a batch OCR submission")
        .with_request("POST", "/v1/ocr/batch")
        .with_body(
            {
                "model": _MODEL,
                "documents": [
                    {
                        "custom_id": "0",
                        "mime_type": "application/pdf",
                        "document_b64": _PDF_B64,
                    }
                ],
            },
            content_type="application/json",
        )
        .will_respond_with(202)
        .with_body(
            {
                # Namespaced "<provider>/<batch_job_id>" — the only field submit() reads.
                "job_id": match.regex("mistral/job-abc", regex=r"[^/]+/.+"),
                "status": "pending",
            },
            content_type="application/json",
        )
    )

    with gateway_consumer_pact.serve() as srv:
        client = GatewayBatchOcrClient(str(srv.url), _MODEL)
        job_id = await client.submit(
            b"%PDF-1.4 contract test", "application/pdf", custom_id="0"
        )

    assert job_id == "mistral/job-abc"


async def test_poll_pending(gateway_consumer_pact):
    (
        gateway_consumer_pact.upon_receiving("a poll for a still-running batch OCR job")
        .given("a pending batch OCR job mistral/job-pending exists")
        .with_request("GET", "/v1/ocr/batch/mistral/job-pending")
        # 202 Accepted while the batch is still processing (terminal polls are 200);
        # the client keys off the body status, so it treats either as "keep polling".
        .will_respond_with(202)
        .with_body({"status": "pending"}, content_type="application/json")
    )

    with gateway_consumer_pact.serve() as srv:
        result = await GatewayBatchOcrClient(str(srv.url), _MODEL).poll(
            "mistral/job-pending"
        )

    assert result.is_pending


async def test_poll_succeeded_returns_pages(gateway_consumer_pact):
    (
        gateway_consumer_pact.upon_receiving("a poll for a succeeded batch OCR job")
        .given("a succeeded batch OCR job mistral/job-done exists")
        .with_request("GET", "/v1/ocr/batch/mistral/job-done")
        .will_respond_with(200)
        .with_body(
            {
                "status": "succeeded",
                "results": [
                    {
                        "custom_id": "0",
                        "pages": [
                            {
                                "index": match.integer(0),
                                "markdown": match.string("# Page one"),
                            }
                        ],
                    }
                ],
            },
            content_type="application/json",
        )
    )

    with gateway_consumer_pact.serve() as srv:
        result = await GatewayBatchOcrClient(str(srv.url), _MODEL).poll(
            "mistral/job-done"
        )

    assert result.is_succeeded
    # pages are (index, markdown, blocks) — blocks is None for a markdown-only
    # backend (Mistral) that emits no layout geometry.
    assert result.pages == [(0, "# Page one", None)]


async def test_poll_succeeded_returns_pages_with_bboxes(gateway_consumer_pact):
    """A layout-aware backend (surya) returns per-block ``bbox`` (normalized [0,1])
    in each page's ``blocks``; the client carries them through for chunk attribution.
    Pins that the optional geometry is present + shaped as the consumer reads it."""
    (
        gateway_consumer_pact.upon_receiving(
            "a poll for a succeeded batch OCR job with layout bboxes"
        )
        .given("a succeeded batch OCR job mistral/job-bbox with layout blocks exists")
        .with_request("GET", "/v1/ocr/batch/mistral/job-bbox")
        .will_respond_with(200)
        .with_body(
            {
                "status": "succeeded",
                "results": [
                    {
                        "custom_id": "0",
                        "pages": [
                            {
                                "index": match.integer(0),
                                "markdown": match.string("Heading"),
                                "blocks": match.each_like(
                                    {
                                        # Fixed-length [x0,y0,x1,y1] normalized [0,1].
                                        "bbox": [
                                            match.number(0.11),
                                            match.number(0.1),
                                            match.number(0.4),
                                            match.number(0.13),
                                        ],
                                        "html": match.string("<h1>Heading</h1>"),
                                    }
                                ),
                            }
                        ],
                    }
                ],
            },
            content_type="application/json",
        )
    )

    with gateway_consumer_pact.serve() as srv:
        result = await GatewayBatchOcrClient(str(srv.url), _MODEL).poll(
            "mistral/job-bbox"
        )

    assert result.is_succeeded
    # One page carrying its blocks list (third tuple element).
    idx, markdown, blocks = result.pages[0]
    assert idx == 0 and blocks and len(blocks[0]["bbox"]) == 4


async def test_poll_failed_surfaces_error(gateway_consumer_pact):
    (
        gateway_consumer_pact.upon_receiving("a poll for a failed batch OCR job")
        .given("a failed batch OCR job mistral/job-failed exists")
        .with_request("GET", "/v1/ocr/batch/mistral/job-failed")
        .will_respond_with(200)
        .with_body(
            {"status": "failed", "error": match.string("batch job failed")},
            content_type="application/json",
        )
    )

    with gateway_consumer_pact.serve() as srv:
        result = await GatewayBatchOcrClient(str(srv.url), _MODEL).poll(
            "mistral/job-failed"
        )

    assert result.is_failed
    assert result.error == "batch job failed"
