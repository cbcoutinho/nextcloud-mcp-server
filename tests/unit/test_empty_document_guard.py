"""Regression guards for card #1230 — empty document payloads.

A document that downloads to zero bytes used to travel all the way to the
embedding gateway, which rejected the batch OCR submission with HTTP 422
``"document decodes to empty bytes"``. 422 is permanent, so the document was
dead-lettered under the generic ``error`` reason and the loss was invisible
outside one dashboard panel.

Two guards close it, and both are pinned here: the ingest path refuses an empty
download before any tier parses it, and the OCR processor refuses an empty
payload before any gateway round-trip — whichever caller handed it the bytes.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from nextcloud_mcp_server.document_processors.base import EMPTY_DOCUMENT_REASON
from nextcloud_mcp_server.document_processors.ocr import OcrProcessor
from nextcloud_mcp_server.vector import processor as proc

pytestmark = pytest.mark.unit

_TRUNCATED = "astrolabe_document_download_truncated_total"


def _task(size_bytes: int | None) -> SimpleNamespace:
    return SimpleNamespace(doc_type="file", size_bytes=size_bytes)


def _source(size: int) -> SimpleNamespace:
    return SimpleNamespace(size=size)


def test_non_empty_download_passes_through():
    assert proc.empty_download_result(_task(1024), _source(1024), "/doc.pdf") is None


def test_text_doc_types_have_no_source():
    """Notes/deck cards carry no binary — the guard must not fire on them."""
    assert proc.empty_download_result(_task(None), None, None) is None


def test_empty_download_of_a_measured_file_is_retryable(metric_sample):
    """The scanner saw bytes, so THIS response is wrong, not the file.

    Each tier is a separate procrastinate job and re-downloads the document, so
    an empty body here is transient. Raising re-queues it (bounded by the
    consecutive-failure counter) instead of dead-lettering a healthy document.
    """
    before = metric_sample(_TRUNCATED, {})

    with pytest.raises(httpx.RemoteProtocolError, match="scanner saw 4096 bytes"):
        proc.empty_download_result(_task(4096), _source(0), "/doc.pdf")

    # Counted on the same panel as the Content-Length short read: both are the
    # server returning fewer bytes than it should have, and a retryable failure
    # nothing counts is exactly the invisibility this change closes.
    assert metric_sample(_TRUNCATED, {}) == before + 1


def test_genuinely_empty_file_fails_with_a_named_reason():
    result = proc.empty_download_result(_task(0), _source(0), "/empty.pdf")

    assert result is not None
    assert result.success is False
    assert result.metadata["parse_failed_reason"] == EMPTY_DOCUMENT_REASON


async def test_ocr_refuses_an_empty_payload_without_calling_the_gateway(mocker):
    """The backstop: no empty submission ever reaches the gateway's 422."""
    processor = OcrProcessor()
    submit = mocker.patch.object(
        processor, "_get_batch_client", side_effect=AssertionError("submitted!")
    )

    result = await processor.process(b"", "application/pdf", "/empty.pdf")

    assert result.success is False
    assert result.metadata["parse_failed_reason"] == EMPTY_DOCUMENT_REASON
    submit.assert_not_called()
