"""Unit tests for the tiered PDF routing in ProcessorRegistry.

Covers: default fast-tier routing, the pymupdf rollback toggle, classification
recording derived from the extraction, and OCR escalation (on/off).
"""

from unittest.mock import MagicMock, call

import pytest

from nextcloud_mcp_server.document_processors import registry as reg_mod
from nextcloud_mcp_server.document_processors.base import (
    DocumentProcessor,
    ProcessingResult,
)
from nextcloud_mcp_server.document_processors.escalation import EscalationDecision
from nextcloud_mcp_server.document_processors.registry import ProcessorRegistry
from tests.fixtures.glyph_corruption import GLYPH_CORRUPT_TEXT

pytestmark = pytest.mark.unit


class _Fake(DocumentProcessor):
    def __init__(
        self,
        name: str,
        tier: str,
        # >= MIN_PAGE_CHARS of clean, whitespace-separated prose so the default
        # classifies "fast" (a shorter string trips the near-empty OCR signal).
        text: str = "this is clean readable prose text",
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
        glyph_corruption_ratio=0.02,
        # Guard off by default so existing tiering tests are unaffected; tests
        # that exercise the size guard pass an explicit cap.
        max_pdf_size_mb=0.0,
    ):
        self.document_tier1_engine = engine
        self.document_classify_enabled = classify
        # ``ocr`` enables the single OCR tier (document_ocr_enabled). The model
        # attr lets build_ocr_backend resolve the OCR model id.
        self.document_ocr_enabled = ocr
        self.document_ocr_model = "mistral/mistral-ocr-latest"
        self.document_ocr_min_text_quality = min_text_quality
        self.document_ocr_page_fraction = page_fraction
        self.document_ocr_min_page_chars = min_page_chars
        self.document_ocr_detect_scanned = detect_scanned
        self.document_glyph_corruption_ratio = glyph_corruption_ratio
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


def test_oversize_result_for_size_matches_byte_form(monkeypatch):
    """The size-only guard must be behaviourally identical to the bytes form.

    ``oversize_result_for_size`` is what the pre-download gate calls, so any
    divergence would mean a document rejected before the fetch behaves
    differently from one rejected after it.
    """
    settings = _Settings(max_pdf_size_mb=0.001)
    r = _registry((_Fake("fast", "fast"), 20))
    content = b"%PDF-1.7" + b"0" * 2048

    by_bytes = r._oversize_result(content, "big.pdf", settings)
    by_size = r.oversize_result_for_size(len(content), "big.pdf", settings)

    assert by_bytes is not None and by_size is not None
    assert by_size.success is by_bytes.success is False
    assert by_size.metadata == by_bytes.metadata
    assert by_size.processor == by_bytes.processor == "size_guard"
    assert by_size.error == by_bytes.error


def test_oversize_result_for_size_passes_under_cap_and_when_disabled():
    r = _registry((_Fake("fast", "fast"), 20))

    under = r.oversize_result_for_size(1024, "small.pdf", _Settings(max_pdf_size_mb=50))
    disabled = r.oversize_result_for_size(
        10 * 1024 * 1024 * 1024, "huge.pdf", _Settings(max_pdf_size_mb=0)
    )

    assert under is None
    assert disabled is None, "cap of 0 disables the guard"


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


# --- glyph-corruption escalation + full-ladder parity ------------------------

# A fast-tier text layer that looks like words (HIGH text_quality) but leaks C0
# control chars -- the broken-/ToUnicode signature the control-char ratio catches.
# Shared with the classifier tests so the two can't diverge.
_GLYPH = GLYPH_CORRUPT_TEXT


async def test_glyph_corrupt_escalates_fast_to_structured(monkeypatch):
    # Not gated on OCR: structured is free + in-cluster, so a glyph-corrupt layer
    # escalates fast->structured even with OCR disabled.
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=False))
    esc = MagicMock()
    monkeypatch.setattr(reg_mod, "record_document_escalation", esc)
    r = _registry(
        (_Fake("fast", "fast", text=_GLYPH), 20),
        (_Fake("structured", "structured", text="clean recovered prose text"), 10),
    )
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "structured"
    esc.assert_called_once_with("fast", "structured", "corrupt_glyphs")


async def test_glyph_corrupt_no_structured_stays_fast(monkeypatch):
    # No structured processor registered AND OCR off -> nothing to escalate to;
    # keep fast (the inline counterpart of the external "suppressed" outcome).
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=False))
    r = _registry((_Fake("fast", "fast", text=_GLYPH), 20))
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "fast"


async def test_glyph_corrupt_no_structured_falls_through_to_ocr(monkeypatch):
    # Parity with the external path: structured unregistered but OCR enabled ->
    # the glyph-corrupt doc falls through to OCR (not silently kept at fast).
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=True))
    esc = MagicMock()
    monkeypatch.setattr(reg_mod, "record_document_escalation", esc)
    r = _registry(
        (_Fake("fast", "fast", text=_GLYPH), 20),
        (_Fake("ocr", "ocr", text="ocr recovered text"), 5),
    )  # no structured registered
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "ocr"
    esc.assert_called_once_with("fast", "ocr", "corrupt_glyphs")


async def test_inline_lowconf_tries_structured_before_ocr(monkeypatch):
    # Full-ladder parity with the external path: a junk-but-non-empty fast layer
    # tries structured (fast->structured) BEFORE any OCR, even with OCR enabled.
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=True))
    esc = MagicMock()
    monkeypatch.setattr(reg_mod, "record_document_escalation", esc)
    r = _registry(
        (_Fake("fast", "fast", text="x" * 40), 20),  # one long token -> quality ~0
        (_Fake("structured", "structured", text="clean recovered prose text here"), 10),
        (_Fake("ocr", "ocr", text="ocr text"), 5),
    )
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "structured"
    esc.assert_called_once_with("fast", "structured", "low_confidence")


async def test_inline_empty_skips_structured_straight_to_ocr(monkeypatch):
    # The one intended shortcut: a scanned/no-text-layer doc (total_chars == 0)
    # skips structured (it cannot extract text from a raster) and goes to OCR.
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=True))
    esc = MagicMock()
    monkeypatch.setattr(reg_mod, "record_document_escalation", esc)
    r = _registry(
        (_Fake("fast", "fast", text=""), 20),
        (_Fake("structured", "structured", text="should not run"), 10),
        (_Fake("ocr", "ocr", text="ocr text"), 5),
    )
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "ocr"
    esc.assert_called_once_with("fast", "ocr", "empty_text")


async def test_inline_fast_structured_ocr_cascade(monkeypatch):
    # Full cascade: a junk-but-non-empty fast layer hops to structured, the
    # structured re-extract is empty (a doc that was ALSO scanned), so it then
    # hops to OCR. The second hop must be attributed from_tier="structured",
    # NOT a second "fast" escalation.
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=True))
    esc = MagicMock()
    monkeypatch.setattr(reg_mod, "record_document_escalation", esc)
    r = _registry(
        (_Fake("fast", "fast", text="x" * 40), 20),  # quality ~0, non-empty
        (_Fake("structured", "structured", text=""), 10),  # re-extract empty
        (_Fake("ocr", "ocr", text="ocr recovered text"), 5),
    )
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "ocr"
    assert esc.call_args_list == [
        call("fast", "structured", "low_confidence"),
        call("structured", "ocr", "empty_text"),
    ]


async def test_inline_structured_still_corrupt_escalates_to_ocr(monkeypatch):
    # Edge: the structured re-extract is ALSO glyph-corrupt (pymupdf also failed to
    # decode). Re-classification stays "structured", so the OCR gate fires --
    # attributed from_tier="structured" with reason corrupt_glyphs.
    monkeypatch.setattr(reg_mod, "get_settings", lambda: _Settings(ocr=True))
    esc = MagicMock()
    monkeypatch.setattr(reg_mod, "record_document_escalation", esc)
    r = _registry(
        (_Fake("fast", "fast", text=_GLYPH), 20),
        (_Fake("structured", "structured", text=_GLYPH), 10),  # still corrupt
        (_Fake("ocr", "ocr", text="ocr recovered text"), 5),
    )
    res = await r.process(b"%PDF-1.7", "application/pdf")
    assert res.processor == "ocr"
    assert esc.call_args_list == [
        call("fast", "structured", "corrupt_glyphs"),
        call("structured", "ocr", "corrupt_glyphs"),
    ]


def test_evaluate_escalation_glyph_corrupt_goes_structured(monkeypatch):
    # External path mirrors the inline path: glyph-corrupt -> structured, never OCR.
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("structured", "structured"), 10),
        (_Fake("ocr", "ocr"), 5),
    )
    res = ProcessingResult(
        text=_GLYPH,
        metadata={
            "page_count": 1,
            "page_boundaries": [
                {"page": 1, "start_offset": 0, "end_offset": len(_GLYPH)}
            ],
        },
        processor="fast",
    )
    decision = r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=True))
    assert decision == EscalationDecision("hop", "structured", "corrupt_glyphs")


def test_evaluate_escalation_glyph_corrupt_no_structured_falls_through_to_ocr(
    monkeypatch,
):
    # External path with structured unregistered: next_available_tier skips the
    # missing rung and lands on OCR, keeping the corrupt_glyphs reason.
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("ocr", "ocr"), 5),
    )  # no structured registered
    res = ProcessingResult(
        text=_GLYPH,
        metadata={
            "page_count": 1,
            "page_boundaries": [
                {"page": 1, "start_offset": 0, "end_offset": len(_GLYPH)}
            ],
        },
        processor="fast",
    )
    decision = r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=True))
    assert decision == EscalationDecision("hop", "ocr", "corrupt_glyphs")


def test_evaluate_escalation_glyph_corrupt_no_structured_ocr_disabled_suppressed(
    monkeypatch,
):
    # Structured unregistered AND OCR registered-but-disabled: the would-be OCR
    # fallthrough is suppressed, and it carries the corrupt_glyphs reason (so the
    # "what-if OCR" counter can show latent glyph-corruption demand).
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("ocr", "ocr"), 5),
    )  # structured not registered; ocr registered but disabled below
    res = ProcessingResult(
        text=_GLYPH,
        metadata={
            "page_count": 1,
            "page_boundaries": [
                {"page": 1, "start_offset": 0, "end_offset": len(_GLYPH)}
            ],
        },
        processor="fast",
    )
    decision = r.evaluate_escalation(res, b"%PDF", "fast", _Settings(ocr=False))
    assert decision == EscalationDecision("suppressed", "ocr", "corrupt_glyphs")


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
    assert r.next_available_tier("fast", s_ocr) == "structured"
    assert r.next_available_tier("structured", s_ocr) == "ocr"
    assert r.next_available_tier("fast", s_ocr, minimum="ocr") == "ocr"
    # The OCR tier is the top of the ladder -> terminal.
    assert r.next_available_tier("ocr", s_ocr) is None


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
    IS registered: with OCR off it suppresses to the OCR tier, never hops to
    structured (a text extractor can't conjure text from a raster scan)."""
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


# --- single OCR tier (external path) -----------------------------------------


def _empty_result() -> ProcessingResult:
    return ProcessingResult(
        text="",
        metadata={
            "page_count": 1,
            "page_boundaries": [{"page": 1, "start_offset": 0, "end_offset": 0}],
        },
        processor="fast",
    )


def test_evaluate_escalation_empty_text_hops_to_ocr(monkeypatch):
    """External path: empty text targets the OCR tier when it is enabled +
    registered."""
    monkeypatch.setattr(reg_mod, "record_document_classification", MagicMock())
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("structured", "structured"), 10),
        (_Fake("ocr", "ocr"), 5),
    )
    decision = r.evaluate_escalation(
        _empty_result(), b"%PDF", "fast", _Settings(ocr=True)
    )
    assert decision == EscalationDecision("hop", "ocr", "empty_text")


def test_evaluate_escalation_ignore_ocr_enabled_computes_ideal_target():
    """The ideal-target walk (ignore_ocr_enabled) resolves the OCR tier even when
    the OCR-enabled gate is off -- this is what backs the suppressed what-if-OCR
    signal."""
    r = _registry(
        (_Fake("fast", "fast"), 20),
        (_Fake("structured", "structured"), 10),
        (_Fake("ocr", "ocr"), 5),
    )
    s = _Settings(ocr=False)
    # OCR gated off -> structured is the only available target above structured's
    # predecessor; the OCR tier is terminal when disabled.
    assert r.next_available_tier("structured", s) is None
    # ...but the *ideal* target ignoring the enable gate is the OCR tier.
    assert r.next_available_tier("structured", s, ignore_ocr_enabled=True) == "ocr"
