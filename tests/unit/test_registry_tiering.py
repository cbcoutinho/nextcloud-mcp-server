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
from nextcloud_mcp_server.document_processors.escalation import EscalationDecision
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
        # Guard off by default so existing tiering tests are unaffected; tests
        # that exercise the size guard pass an explicit cap.
        max_pdf_size_mb=0.0,
    ):
        self.document_tier1_engine = engine
        self.document_classify_enabled = classify
        self.document_ocr_enabled = ocr
        self.document_ocr_min_text_quality = min_text_quality
        self.document_ocr_page_fraction = page_fraction
        self.document_ocr_min_page_chars = min_page_chars
        self.document_ocr_detect_scanned = detect_scanned
        self.document_max_pdf_size_mb = max_pdf_size_mb


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


async def test_oversize_pdf_fails_fast_without_parsing(monkeypatch):
    """A PDF over the size cap must fail fast as 'oversize' before any tier runs."""
    monkeypatch.setattr(
        reg_mod, "get_settings", lambda: _Settings(max_pdf_size_mb=0.001)
    )
    fast = _Fake("fast", "fast")
    ran = False
    orig = fast.process

    async def _tracking(*a, **k):
        nonlocal ran
        ran = True
        return await orig(*a, **k)

    fast.process = _tracking  # type: ignore[method-assign]
    r = _registry((fast, 20))

    # ~2 KB > 0.001 MB (~1 KB) cap.
    res = await r.process(b"%PDF-1.7" + b"0" * 2048, "application/pdf", "big.pdf")

    assert res.success is False
    assert res.metadata["parse_failed_reason"] == "oversize"
    assert res.processor == "size_guard"
    assert ran is False, "size guard must short-circuit before the fast tier runs"


async def test_under_cap_pdf_still_parses(monkeypatch):
    """A PDF under the cap is unaffected by the guard."""
    monkeypatch.setattr(
        reg_mod, "get_settings", lambda: _Settings(max_pdf_size_mb=10.0)
    )
    r = _registry((_Fake("fast", "fast"), 20))
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.success is True
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


# --- Per-tier external path (Deck #323) -------------------------------------


async def test_process_tier_runs_named_tier(monkeypatch):
    """process_tier runs exactly the requested tier's processor, not priority."""
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings())
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("structured", "structured"), 10),
        (_Fake("ocr", "ocr"), 5),
    )
    res = await r.process_tier(b"%PDF-1.7", "application/pdf", "f.pdf", "structured")
    assert res.processor == "structured"


async def test_process_tier_unknown_tier_raises(monkeypatch):
    from nextcloud_mcp_server.document_processors.base import ProcessorError

    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings())
    r = _registry((_Fake("fast", "fast"), 20))
    with pytest.raises(ProcessorError, match="structured"):
        await r.process_tier(b"%PDF-1.7", "application/pdf", "f.pdf", "structured")


async def test_process_tier_oversize_fails_fast(monkeypatch):
    """The size guard applies on the per-tier path too (before any parse)."""
    monkeypatch.setattr(
        reg_mod, "get_settings", lambda: _Settings(max_pdf_size_mb=0.001)
    )
    r = _registry((_Fake("ocr", "ocr"), 5))
    res = await r.process_tier(b"x" * 4096, "application/pdf", "big.pdf", "ocr")
    assert res.success is False
    assert res.metadata["parse_failed_reason"] == "oversize"


def test_next_available_tier_walks_ladder():
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("structured", "structured"), 10),
        (_Fake("ocr", "ocr"), 5),
    )
    # ocr disabled -> structured is the only target above fast.
    s = _Settings(ocr=False)
    assert r.next_available_tier("fast", s) == "structured"
    assert r.next_available_tier("structured", s) is None  # ocr gated off
    # ocr enabled -> reachable; minimum skips the structured rung.
    s_ocr = _Settings(ocr=True)
    assert r.next_available_tier("structured", s_ocr) == "ocr"
    assert r.next_available_tier("fast", s_ocr, minimum="ocr") == "ocr"


def test_next_available_tier_skips_unregistered():
    # No structured processor -> fast escalates straight to ocr.
    r = _registry((_Fake("fast", "fast"), 20), (_Fake("ocr", "ocr"), 5))
    assert r.next_available_tier("fast", _Settings(ocr=True)) == "ocr"


def test_evaluate_escalation_good_text_indexes(monkeypatch):
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry(
        (_Fake("fast", "fast", text="This is clean readable prose text."), 20),
        (_Fake("ocr", "ocr"), 5),
    )
    res = ProcessingResult(
        text="This is clean readable prose text.",
        metadata={
            "page_count": 1,
            "page_boundaries": [{"page": 1, "start_offset": 0, "end_offset": 34}],
        },
        processor="fast",
    )
    assert r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=True)) is None


def test_evaluate_escalation_empty_jumps_to_ocr(monkeypatch):
    """A scanned (no-text-layer) result targets ocr directly, skipping structured."""
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("structured", "structured"), 10),
        (_Fake("ocr", "ocr"), 5),
    )
    res = ProcessingResult(
        text="",
        metadata={
            "page_count": 1,
            "page_boundaries": [{"page": 1, "start_offset": 0, "end_offset": 0}],
        },
        processor="fast",
    )
    decision = r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=True))
    assert decision == EscalationDecision("hop", "ocr", "empty_text")


def test_evaluate_escalation_lowconf_goes_to_structured(monkeypatch):
    """A junk-but-non-empty layer escalates to the next rung (structured)."""
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    junk = "x" * 40  # one long token, no whitespace -> quality ~0
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("structured", "structured"), 10),
        (_Fake("ocr", "ocr"), 5),
    )
    res = ProcessingResult(
        text=junk,
        metadata={
            "page_count": 1,
            "page_boundaries": [
                {"page": 1, "start_offset": 0, "end_offset": len(junk)}
            ],
        },
        processor="fast",
    )
    decision = r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=True))
    assert decision == EscalationDecision("hop", "structured", "low_confidence")


def test_evaluate_escalation_failure_not_escalated(monkeypatch):
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry((_Fake("fast", "fast"), 20), (_Fake("ocr", "ocr"), 5))
    res = ProcessingResult(
        text="",
        metadata={"parse_failed_reason": "error"},
        processor="fast",
        success=False,
    )
    assert r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=True)) is None


def test_evaluate_escalation_terminal_when_no_higher_tier(monkeypatch):
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    # Only fast registered -> nowhere to escalate even on junk text.
    r = _registry((_Fake("fast", "fast"), 20))
    junk = "y" * 40
    res = ProcessingResult(
        text=junk,
        metadata={
            "page_count": 1,
            "page_boundaries": [
                {"page": 1, "start_offset": 0, "end_offset": len(junk)}
            ],
        },
        processor="fast",
    )
    assert r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=True)) is None


def test_evaluate_escalation_zero_page_does_not_escalate(monkeypatch):
    """A zero-page (empty/corrupt) PDF never escalates on the external path."""
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry((_Fake("fast", "fast"), 20), (_Fake("ocr", "ocr"), 5))
    res = ProcessingResult(
        text="",
        metadata={"page_count": 0, "page_boundaries": []},
        processor="fast",
    )
    assert r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=True)) is None


def test_evaluate_escalation_lowconf_to_ocr_when_no_structured(monkeypatch):
    """fast+ocr only: a low-confidence parse routes straight to ocr (skips the
    unregistered structured rung), not to None."""
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    junk = "z" * 40  # non-empty but junk -> recommended ocr, total_chars > 0
    r = _registry((_Fake("fast", "fast"), 20), (_Fake("ocr", "ocr"), 5))
    res = ProcessingResult(
        text=junk,
        metadata={
            "page_count": 1,
            "page_boundaries": [
                {"page": 1, "start_offset": 0, "end_offset": len(junk)}
            ],
        },
        processor="fast",
    )
    decision = r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=True))
    assert decision == EscalationDecision("hop", "ocr", "low_confidence")


def test_evaluate_escalation_suppressed_when_ocr_disabled(monkeypatch):
    """OCR off: a scanned doc does NOT hop to ocr; it returns a 'suppressed'
    decision (the what-if-OCR signal) so the caller indexes at the current tier."""
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry((_Fake("fast", "fast"), 20), (_Fake("ocr", "ocr"), 5))
    res = ProcessingResult(
        text="",
        metadata={
            "page_count": 1,
            "page_boundaries": [{"page": 1, "start_offset": 0, "end_offset": 0}],
        },
        processor="fast",
    )
    decision = r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=False))
    assert decision == EscalationDecision("suppressed", "ocr", "empty_text")


def test_evaluate_escalation_lowconf_suppressed_when_only_ocr_disabled(monkeypatch):
    """fast+ocr only, OCR off, junk text: the next rung is the disabled ocr, so
    the would-be hop is suppressed (not a structured hop, which isn't registered)."""
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    junk = "q" * 40
    r = _registry((_Fake("fast", "fast"), 20), (_Fake("ocr", "ocr"), 5))
    res = ProcessingResult(
        text=junk,
        metadata={
            "page_count": 1,
            "page_boundaries": [
                {"page": 1, "start_offset": 0, "end_offset": len(junk)}
            ],
        },
        processor="fast",
    )
    decision = r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=False))
    assert decision == EscalationDecision("suppressed", "ocr", "low_confidence")


def test_evaluate_escalation_structured_hop_not_suppressed_when_ocr_off(monkeypatch):
    """OCR off but structured available + junk text → real hop to structured
    (not suppressed): the in-cluster rung can still run."""
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    junk = "w" * 40
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("structured", "structured"), 10),
        (_Fake("ocr", "ocr"), 5),
    )
    res = ProcessingResult(
        text=junk,
        metadata={
            "page_count": 1,
            "page_boundaries": [
                {"page": 1, "start_offset": 0, "end_offset": len(junk)}
            ],
        },
        processor="fast",
    )
    decision = r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=False))
    assert decision == EscalationDecision("hop", "structured", "low_confidence")


def test_evaluate_escalation_terminal_when_ocr_unregistered_and_off(monkeypatch):
    """No OCR processor registered at all (not merely disabled) → genuinely
    terminal: returns None, NOT a suppressed decision. 'Absent' != 'disabled'."""
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry((_Fake("fast", "fast"), 20))  # only fast; no ocr processor
    res = ProcessingResult(
        text="",
        metadata={
            "page_count": 1,
            "page_boundaries": [{"page": 1, "start_offset": 0, "end_offset": 0}],
        },
        processor="fast",
    )
    assert r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=False)) is None


def test_evaluate_escalation_empty_suppressed_even_when_structured_registered(
    monkeypatch,
):
    """empty_text uses minimum='ocr', so it skips structured even when structured
    IS registered: with OCR off it suppresses to ocr, never hops to structured
    (a text extractor can't conjure text from a raster scan)."""
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("structured", "structured"), 10),  # registered but skipped for empty
        (_Fake("ocr", "ocr"), 5),
    )
    res = ProcessingResult(
        text="",
        metadata={
            "page_count": 1,
            "page_boundaries": [{"page": 1, "start_offset": 0, "end_offset": 0}],
        },
        processor="fast",
    )
    decision = r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=False))
    assert decision == EscalationDecision("suppressed", "ocr", "empty_text")
