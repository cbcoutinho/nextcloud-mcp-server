"""Unit tests for the tiered PDF routing in ProcessorRegistry.

Covers: default fast-tier routing, the pymupdf rollback toggle, classification
recording derived from the extraction, and OCR escalation (on/off).
"""

from unittest.mock import MagicMock

import pytest

from nextcloud_mcp_server.document_processors import registry as reg_mod
from nextcloud_mcp_server.document_processors.base import (
    DocumentProcessor,
    ProcessingResult,
)
from nextcloud_mcp_server.document_processors.registry import ProcessorRegistry

pytestmark = pytest.mark.unit


class _Fake(DocumentProcessor):
    def __init__(
        self,
        name: str,
        tier: str,
        text: str = "clean text here",
        success=True,
        pages: int = 1,
    ):
        self._name = name
        self._tier = tier
        self._text = text
        self._success = success
        self._pages = pages

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
        self, content, content_type, filename=None, options=None, progress_callback=None
    ):
        boundaries = (
            [{"page": 1, "start_offset": 0, "end_offset": len(self._text)}]
            if self._pages
            else []
        )
        return ProcessingResult(
            text=self._text,
            metadata={"page_count": self._pages, "page_boundaries": boundaries},
            processor=self._name,
            success=self._success,
        )

    async def health_check(self) -> bool:
        return True


class _Settings:
    def __init__(
        self,
        engine="pypdfium2",
        classify=True,
        ocr=False,
        min_text_quality=0.5,
        page_fraction=0.5,
        min_page_chars=16,
        detect_scanned=False,
    ):
        self.document_tier1_engine = engine
        self.document_classify_enabled = classify
        self.document_ocr_enabled = ocr
        self.document_ocr_min_text_quality = min_text_quality
        self.document_ocr_page_fraction = page_fraction
        self.document_ocr_min_page_chars = min_page_chars
        self.document_ocr_detect_scanned = detect_scanned


def _registry(*procs: tuple[DocumentProcessor, int]) -> ProcessorRegistry:
    r = ProcessorRegistry()
    for proc, prio in procs:
        r.register(proc, priority=prio)
    return r


async def test_pdf_routes_to_fast_tier(monkeypatch):
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings())
    r = _registry((_Fake("fast", "fast"), 20), (_Fake("structured", "structured"), 10))
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "fast"


async def test_engine_rollback_uses_structured(monkeypatch):
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(engine="pymupdf"))
    r = _registry((_Fake("fast", "fast"), 20), (_Fake("structured", "structured"), 10))
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "structured"


async def test_engine_rollback_warns_when_no_structured(monkeypatch, caplog):
    # pymupdf rollback with no structured processor registered: it falls back to
    # the fast processor but must warn (it silently used what the user opted out
    # of otherwise).
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(engine="pymupdf"))
    r = _registry((_Fake("fast", "fast"), 20))
    with caplog.at_level(
        "WARNING", logger="nextcloud_mcp_server.document_processors.registry"
    ):
        res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "fast"
    assert any("no 'structured' processor" in rec.message for rec in caplog.records)


async def test_records_classification(monkeypatch):
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings())
    rec = MagicMock()
    monkeypatch.setattr(reg_mod, "record_document_classification", rec)
    r = _registry((_Fake("fast", "fast"), 20))
    await r.process(b"%PDF-1.7", "application/pdf")
    rec.assert_called_once()
    # recommended_tier, flags, mean_text_quality, ocr_page_fraction all threaded
    # through (the last two feed the per-tenant tuning histograms).
    args = rec.call_args.args
    assert len(args) == 4
    assert isinstance(args[0], str) and isinstance(args[3], float)


async def test_classify_disabled_skips_recording(monkeypatch):
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(classify=False))
    rec = MagicMock()
    monkeypatch.setattr(reg_mod, "record_document_classification", rec)
    r = _registry((_Fake("fast", "fast"), 20))
    await r.process(b"%PDF-1.7", "application/pdf")
    rec.assert_not_called()


async def test_ocr_escalation_on_empty_text(monkeypatch):
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=True))
    esc = MagicMock()
    monkeypatch.setattr(reg_mod, "record_document_escalation", esc)
    r = _registry(
        (_Fake("fast", "fast", text=""), 20),
        (_Fake("ocr", "ocr", text="ocr text"), 5),
    )
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "ocr"
    esc.assert_called_once()


async def test_zero_page_pdf_does_not_escalate(monkeypatch):
    # An empty/corrupt PDF (no pages) classifies "ocr" but must NOT escalate --
    # OCR can't help and it would be wasteful.
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=True))
    esc = MagicMock()
    monkeypatch.setattr(reg_mod, "record_document_escalation", esc)
    r = _registry(
        (_Fake("fast", "fast", text="", pages=0), 20),
        (_Fake("ocr", "ocr"), 5),
    )
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "fast"
    esc.assert_not_called()


async def test_pipeline_tier_stamped_on_metadata(monkeypatch):
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings())
    r = _registry((_Fake("fast", "fast"), 20))
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.metadata["pipeline_tier"] == "fast"


async def test_ocr_failure_falls_back_to_fast(monkeypatch):
    # OCR enabled but the backend can't run (no creds / API down) -> keep the
    # tier-1 result instead of failing the document.
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=True))
    monkeypatch.setattr(reg_mod, "record_document_escalation", MagicMock())
    r = _registry(
        (_Fake("fast", "fast", text=""), 20),
        (_Fake("ocr", "ocr", text="", success=False), 5),
    )
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "fast"
    assert res.success is True


async def test_no_ocr_escalation_when_disabled(monkeypatch):
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=False))
    r = _registry(
        (_Fake("fast", "fast", text=""), 20),
        (_Fake("ocr", "ocr"), 5),
    )
    res = await r.process(b"%PDF-1.7", "application/pdf")
    # Fast tier is terminal when OCR is disabled.
    assert res.processor == "fast"
