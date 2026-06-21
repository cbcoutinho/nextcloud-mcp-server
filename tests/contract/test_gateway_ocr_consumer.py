"""Consumer contract: nextcloud-mcp-server -> embedding-gateway SYNC OCR (#387).

The OCR tier's synchronous backend (:class:`_GatewayOcrBackend` in
``document_processors/ocr.py``) POSTs a document to ``POST /v1/ocr`` and reads
per-page ``index`` + ``markdown``, and — when a layout-aware backend (surya) is
configured — per-block ``bbox`` (normalized [0,1]) + ``html`` from ``pages[].blocks``,
which it turns into per-block char spans for pre-computed search highlights.

This pact pins those response shapes for the ``astrolabe-cloud-gateway`` provider
(verified in astrolabe-cloud-website, services/embedding-gateway). Only the fields
the client reads are asserted, so the contract stays additive-safe. Two interactions:
a surya response WITH ``blocks[].bbox`` and a Mistral response WITHOUT blocks (the
pymupdf-fallback trigger). The gateway is unauthenticated today, so no bearer is
sent. See ADR-029.
"""

import base64

import pytest
from pact import match

from nextcloud_mcp_server.document_processors.ocr import _GatewayOcrBackend

pytestmark = pytest.mark.contract

_SURYA_MODEL = "surya/surya-ocr-2"
_MISTRAL_MODEL = "mistral/mistral-ocr-latest"
_DOC = b"%PDF-1.4 contract test"
_DOC_B64 = base64.b64encode(_DOC).decode("ascii")


async def test_sync_ocr_returns_pages_with_bboxes(gateway_consumer_pact):
    """surya: each page carries ``blocks[].bbox`` (normalized [0,1]) + ``html``; the
    backend locates each block's text in the page markdown and emits a char span."""
    (
        gateway_consumer_pact.upon_receiving(
            "a sync OCR request for a surya layout doc"
        )
        .given("the gateway OCRs a document with surya and returns layout bboxes")
        .with_request("POST", "/v1/ocr")
        .with_body(
            {
                "model": _SURYA_MODEL,
                "document_b64": _DOC_B64,
                "mime_type": "application/pdf",
            },
            content_type="application/json",
        )
        .will_respond_with(200)
        .with_body(
            {
                "model": match.string(_SURYA_MODEL),
                # TWO blocks on one page, so the contract makes the join-order
                # invariant explicit: page markdown == block texts joined by "\n\n"
                # in reading order, which is what _pages_to_text relies on to locate
                # each block and emit its char span.
                "pages": [
                    {
                        "index": match.integer(0),
                        "markdown": "Heading\n\nBody text",
                        "blocks": [
                            {
                                # Fixed-length [x0,y0,x1,y1] normalized [0,1] — a
                                # literal 4-list of number matchers (NOT each_like,
                                # which would emit a 1-element example).
                                "bbox": [
                                    match.number(0.11),
                                    match.number(0.1),
                                    match.number(0.4),
                                    match.number(0.13),
                                ],
                                "html": "<h1>Heading</h1>",
                            },
                            {
                                "bbox": [
                                    match.number(0.11),
                                    match.number(0.2),
                                    match.number(0.6),
                                    match.number(0.23),
                                ],
                                "html": "<p>Body text</p>",
                            },
                        ],
                    }
                ],
            },
            content_type="application/json",
        )
    )

    with gateway_consumer_pact.serve() as srv:
        text, boundaries, block_spans = await _GatewayOcrBackend(
            str(srv.url), _SURYA_MODEL
        ).ocr(_DOC, "application/pdf")

    assert text == "Heading\n\nBody text"
    # Both blocks located in reading order -> two spans, each mapping back onto its
    # own text slice (proves the join-order dependency holds end-to-end).
    assert len(block_spans) == 2
    assert (
        text[block_spans[0]["start_offset"] : block_spans[0]["end_offset"]] == "Heading"
    )
    assert (
        text[block_spans[1]["start_offset"] : block_spans[1]["end_offset"]]
        == "Body text"
    )


async def test_sync_ocr_without_blocks_falls_back(gateway_consumer_pact):
    """Mistral: no ``blocks`` -> no block spans (the consumer renders bboxes via
    pymupdf at query time). Pins that a markdown-only response is valid."""
    (
        gateway_consumer_pact.upon_receiving(
            "a sync OCR request for a markdown-only doc"
        )
        .given("the gateway OCRs a document with mistral and returns no layout blocks")
        .with_request("POST", "/v1/ocr")
        .with_body(
            {
                "model": _MISTRAL_MODEL,
                "document_b64": _DOC_B64,
                "mime_type": "application/pdf",
            },
            content_type="application/json",
        )
        .will_respond_with(200)
        .with_body(
            {
                "model": match.string(_MISTRAL_MODEL),
                "pages": [{"index": match.integer(0), "markdown": "Plain text"}],
            },
            content_type="application/json",
        )
    )

    with gateway_consumer_pact.serve() as srv:
        text, _boundaries, block_spans = await _GatewayOcrBackend(
            str(srv.url), _MISTRAL_MODEL
        ).ocr(_DOC, "application/pdf")

    assert text == "Plain text"
    assert block_spans == []
