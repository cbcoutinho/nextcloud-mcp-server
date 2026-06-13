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


def _registry(result: ProcessingResult, decision):
    reg = MagicMock()
    reg.process_tier = AsyncMock(return_value=result)
    reg.evaluate_escalation = MagicMock(return_value=decision)
    return reg


async def test_good_parse_returns_result(monkeypatch):
    rec = MagicMock()
    sup = MagicMock()
    monkeypatch.setattr(processor, "record_document_escalation", rec)
    monkeypatch.setattr(processor, "record_document_escalation_suppressed", sup)
    result = ProcessingResult(text="clean", metadata={}, processor="fast")
    reg = _registry(result, decision=None)
    out = await processor._parse_pdf_tier(
        reg, b"%PDF", "application/pdf", "f.pdf", "fast", settings=object()
    )
    assert out is result
    rec.assert_not_called()
    sup.assert_not_called()


async def test_low_quality_parse_raises_escalate(monkeypatch):
    rec = MagicMock()
    monkeypatch.setattr(processor, "record_document_escalation", rec)
    result = ProcessingResult(text="", metadata={}, processor="fast")
    reg = _registry(result, decision=EscalationDecision("hop", "ocr", "empty_text"))
    with pytest.raises(EscalateError) as ei:
        await processor._parse_pdf_tier(
            reg, b"%PDF", "application/pdf", "f.pdf", "fast", settings=object()
        )
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
    out = await processor._parse_pdf_tier(
        reg, b"%PDF", "application/pdf", "f.pdf", "fast", settings=object()
    )
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
        reg, b"%PDF", "application/pdf", "big.pdf", "fast", settings=object()
    )
    # success=False short-circuits: the gate is never consulted, no escalation.
    assert out is result
    reg.evaluate_escalation.assert_not_called()
    rec.assert_not_called()
    sup.assert_not_called()
