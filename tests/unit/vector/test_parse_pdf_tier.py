"""Unit tests for the per-tier PDF parse + escalation gate (Deck #323/#324).

``processor._parse_pdf_tier`` runs one tier and either indexes the result,
raises ``EscalateError`` (a real queue-hop), or — when the ideal next tier is
disabled (OCR off) — records a suppressed escalation and indexes as terminal.
These exercise the decision without standing up the full ingest pipeline.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.document_processors.base import ProcessingResult
from nextcloud_mcp_server.document_processors.escalation import (
    EscalateError,
    EscalationDecision,
)
from nextcloud_mcp_server.vector import processor

pytestmark = pytest.mark.unit


def _source(content: bytes = b"%PDF", filename: str = "f.pdf"):
    """A file-backed handle standing in for a spooled download."""
    from nextcloud_mcp_server.document_processors.source import MemoryDocumentSource

    return MemoryDocumentSource(content, "application/pdf", filename)


def _registry(result: ProcessingResult, decision):
    reg = MagicMock()
    reg.process_tier_source = AsyncMock(return_value=result)
    reg.evaluate_escalation_source = MagicMock(return_value=decision)
    return reg


async def test_good_parse_returns_result(monkeypatch):
    rec = MagicMock()
    sup = MagicMock()
    monkeypatch.setattr(processor, "record_document_escalation", rec)
    monkeypatch.setattr(processor, "record_document_escalation_suppressed", sup)
    result = ProcessingResult(text="clean", metadata={}, processor="fast")
    reg = _registry(result, decision=None)
    out = await processor._parse_pdf_tier(reg, _source(), "fast", settings=object())
    assert out is result
    rec.assert_not_called()
    sup.assert_not_called()


async def test_low_quality_parse_raises_escalate(monkeypatch):
    rec = MagicMock()
    monkeypatch.setattr(processor, "record_document_escalation", rec)
    result = ProcessingResult(text="", metadata={}, processor="fast")
    reg = _registry(result, decision=EscalationDecision("hop", "ocr", "empty_text"))
    with pytest.raises(EscalateError) as ei:
        await processor._parse_pdf_tier(reg, _source(), "fast", settings=object())
    assert ei.value.from_tier == "fast"
    assert ei.value.to_tier == "ocr"
    assert ei.value.reason == "empty_text"
    # The escalation is recorded at the decision point.
    rec.assert_called_once_with("fast", "ocr", "empty_text")


async def test_suppressed_decision_indexes_without_hop(monkeypatch):
    """OCR-off (suppressed): index this tier's result, record the would-be hop,
    do NOT raise EscalateError."""
    rec = MagicMock()
    sup = MagicMock()
    monkeypatch.setattr(processor, "record_document_escalation", rec)
    monkeypatch.setattr(processor, "record_document_escalation_suppressed", sup)
    result = ProcessingResult(text="junk", metadata={}, processor="fast")
    reg = _registry(
        result, decision=EscalationDecision("suppressed", "ocr", "empty_text")
    )
    out = await processor._parse_pdf_tier(reg, _source(), "fast", settings=object())
    assert out is result  # indexed as terminal, no hop
    sup.assert_called_once_with("fast", "ocr", "empty_text")
    rec.assert_not_called()


async def test_hard_failure_returns_result_without_escalating(monkeypatch):
    rec = MagicMock()
    sup = MagicMock()
    monkeypatch.setattr(processor, "record_document_escalation", rec)
    monkeypatch.setattr(processor, "record_document_escalation_suppressed", sup)
    result = ProcessingResult(
        text="",
        metadata={"parse_failed_reason": "oversize"},
        processor="size_guard",
        success=False,
    )
    reg = _registry(result, decision=EscalationDecision("hop", "ocr", "empty_text"))
    out = await processor._parse_pdf_tier(
        reg, _source(filename="big.pdf"), "fast", settings=object()
    )
    # success=False short-circuits: the gate is never consulted, no escalation.
    assert out is result
    reg.evaluate_escalation_source.assert_not_called()
    rec.assert_not_called()
    sup.assert_not_called()


async def test_ocr_batch_pending_sentinel_raises_batch_pending():
    """Batch OCR (Deck #332): the OCR tier's pending sentinel result is turned
    into a BatchPending raise (same decision point as EscalateError), carrying
    the processor's retry_in, and the escalation gate is never consulted."""
    from nextcloud_mcp_server.document_processors.escalation import BatchPending
    from nextcloud_mcp_server.document_processors.ocr import (
        OCR_BATCH_PENDING_KEY,
        OCR_BATCH_RETRY_IN_KEY,
    )

    result = ProcessingResult(
        text="",
        metadata={OCR_BATCH_PENDING_KEY: True, OCR_BATCH_RETRY_IN_KEY: 90},
        processor="ocr",
        success=False,
    )
    reg = _registry(result, decision=None)
    source = _source(filename="scan.pdf")
    settings = object()

    with pytest.raises(BatchPending) as ei:
        await processor._parse_pdf_tier(reg, source, "ocr", settings=settings)
    assert ei.value.retry_in == 90
    reg.evaluate_escalation_source.assert_not_called()


async def test_options_threaded_to_process_tier():
    """The OCR identity options are forwarded to process_tier (batch needs them)."""
    result = ProcessingResult(text="clean", metadata={}, processor="ocr")
    reg = _registry(result, decision=None)
    opts = {"user_id": "u", "doc_id": "d", "doc_type": "file", "etag": "v"}
    await processor._parse_pdf_tier(
        reg, _source(), "ocr", settings=object(), options=opts
    )
    # process_tier_source(source, tier, options=...)
    assert reg.process_tier_source.await_args.kwargs["options"] == opts
