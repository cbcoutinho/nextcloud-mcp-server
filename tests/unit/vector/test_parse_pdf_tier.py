"""Unit tests for the per-tier PDF parse + escalation gate (Deck #323/#324).

``processor._parse_pdf_tier`` runs one tier and either indexes the result,
raises ``EscalateError`` (a real queue-hop), or — when the ideal next tier is
disabled (OCR off) — records a suppressed escalation and indexes as terminal.
These exercise the decision without standing up the full ingest pipeline.
"""

from types import SimpleNamespace
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
    from nextcloud_mcp_server.document_source import MemoryDocumentSource

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


# --- standalone images on the OCR tier (#1882) --------------------------------


def _image_source():
    from nextcloud_mcp_server.document_source import MemoryDocumentSource

    return MemoryDocumentSource(b"\x89PNG", "image/png", "shot.png")


@pytest.fixture
def ocr_images_registry(monkeypatch):
    """A real registry whose OCR tier claims images, with the backend call mocked."""
    from nextcloud_mcp_server.document_processors import ocr
    from nextcloud_mcp_server.document_processors.registry import ProcessorRegistry

    monkeypatch.setattr(
        ocr,
        "get_settings",
        lambda: SimpleNamespace(document_ocr_enabled=True, document_ocr_images=True),
    )
    ocr_processor = ocr.OcrProcessor()
    process = AsyncMock(
        return_value=ProcessingResult(
            text="receipt total 42", metadata={}, processor="ocr"
        )
    )
    monkeypatch.setattr(ocr_processor, "process", process)
    reg = ProcessorRegistry()
    reg.register(ocr_processor, priority=1)
    return reg, process


@pytest.mark.parametrize("tier", ["fast", "structured"])
async def test_image_below_ocr_tier_hops_to_ocr_unparsed(
    monkeypatch, ocr_images_registry, tier
):
    rec = MagicMock()
    monkeypatch.setattr(processor, "record_document_escalation", rec)
    reg, process = ocr_images_registry

    with pytest.raises(EscalateError) as ei:
        await processor._parse_pdf_tier(reg, _image_source(), tier, settings=object())

    assert (ei.value.from_tier, ei.value.to_tier, ei.value.reason) == (
        tier,
        "ocr",
        "image",
    )
    rec.assert_called_once_with(tier, "ocr", "image")
    process.assert_not_called()


async def test_image_on_ocr_tier_runs_ocr_processor(monkeypatch, ocr_images_registry):
    """On the OCR tier the image runs through the OCR processor and is indexed
    as-is: the PDF quality gate is not consulted."""
    reg, process = ocr_images_registry
    gate = MagicMock()
    monkeypatch.setattr(reg, "evaluate_escalation_source", gate)
    opts = {"user_id": "u", "doc_id": "d", "doc_type": "file", "etag": "v"}

    out = await processor._parse_pdf_tier(
        reg, _image_source(), "ocr", settings=object(), options=opts
    )

    assert out.text == "receipt total 42"
    process.assert_awaited_once()
    assert process.await_args.args[1] == "image/png"
    gate.assert_not_called()


async def test_image_batch_pending_raises_batch_pending(ocr_images_registry):
    from nextcloud_mcp_server.document_processors.escalation import BatchPending
    from nextcloud_mcp_server.document_processors.ocr import (
        OCR_BATCH_PENDING_KEY,
        OCR_BATCH_RETRY_IN_KEY,
    )

    reg, process = ocr_images_registry
    process.return_value = ProcessingResult(
        text="",
        metadata={OCR_BATCH_PENDING_KEY: True, OCR_BATCH_RETRY_IN_KEY: 30},
        processor="ocr",
        success=False,
    )

    with pytest.raises(BatchPending) as ei:
        await processor._parse_pdf_tier(reg, _image_source(), "ocr", settings=object())
    assert ei.value.retry_in == 30


def test_uses_tier_ladder(ocr_images_registry):
    """PDFs always take the ladder; an image only when the OCR tier is its reader,
    so a higher-priority image processor (Docling, Tesseract) keeps it inline."""
    reg, _ = ocr_images_registry
    assert processor._uses_tier_ladder(reg, "application/pdf")
    assert processor._uses_tier_ladder(reg, "image/png")
    assert not processor._uses_tier_ladder(reg, "text/plain")

    image_reader = MagicMock()
    image_reader.name = "docling"
    image_reader.tier = "images"
    image_reader.supports.return_value = True
    reg.register(image_reader, priority=20)
    assert not processor._uses_tier_ladder(reg, "image/png")


@pytest.mark.parametrize(
    "content_type", ["application/pdf; charset=binary", "IMAGE/PNG; q=1"]
)
def test_tier_lookup_ignores_content_type_parameters(ocr_images_registry, content_type):
    """The tier path now passes the source's own content type (not the PDF
    constant) to the processor lookup, so parameters must not break the match."""
    reg, _ = ocr_images_registry
    assert reg._processor_for_tier("ocr", content_type) is not None
