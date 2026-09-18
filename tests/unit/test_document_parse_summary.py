"""What a read tells the caller about a parse that degraded (Deck #894).

``summarize_parse`` is the one place the wording of every degradation lives, so
these tests pin the *statements* -- not just the flags. The failure they guard
against is a read that returns three pages of a 400-page scan and says nothing.
"""

from types import SimpleNamespace

import pytest

from nextcloud_mcp_server.document_processors.base import ProcessingResult
from nextcloud_mcp_server.utils.document_parser import summarize_parse

pytestmark = pytest.mark.unit


def _settings(**overrides) -> SimpleNamespace:
    values = {"document_max_pdf_size_mb": 50.0, "document_markdown_max_pages": 150}
    values.update(overrides)
    return SimpleNamespace(**values)


def _result(text="text", metadata=None, processor="pypdfium2_fast", success=True):
    return ProcessingResult(
        text=text,
        metadata=metadata or {},
        processor=processor,
        success=success,
    )


def test_clean_fast_parse_says_nothing():
    summary = summarize_parse(
        _result(metadata={"pipeline_tier": "fast", "parse_mode": "text_only"}),
        _settings(),
    )

    assert summary.status == "parsed"
    assert summary.tier == "fast"
    assert summary.processor == "pypdfium2_fast"
    assert summary.content_format == "text"
    assert summary.notes == []


def test_markdown_parse_is_labelled_markdown():
    summary = summarize_parse(
        _result(
            text="# Title",
            metadata={"pipeline_tier": "structured", "parse_mode": "markdown"},
            processor="pymupdf",
        ),
        _settings(),
    )

    assert summary.content_format == "markdown"
    assert summary.tier == "structured"
    assert summary.notes == []


def test_page_ceiling_names_the_document_and_the_limit():
    summary = summarize_parse(
        _result(
            metadata={
                "pipeline_tier": "fast",
                "parse_mode": "text_only",
                "markdown_skipped_reason": "page_ceiling",
                "page_count": 412,
            }
        ),
        _settings(),
        markdown_requested=True,
    )

    assert summary.content_format == "text"
    assert "412 pages" in summary.notes[0]
    assert "DOCUMENT_MARKDOWN_MAX_PAGES=150" in summary.notes[0]


def test_markdown_disabled_is_distinguished_from_the_ceiling():
    summary = summarize_parse(
        _result(
            metadata={"parse_mode": "text_only", "markdown_skipped_reason": "disabled"}
        ),
        _settings(document_markdown_max_pages=0),
        markdown_requested=True,
    )

    assert "DOCUMENT_MARKDOWN_MAX_PAGES=0" in summary.notes[0]


def test_markdown_promotion_failure_is_stated():
    summary = summarize_parse(
        _result(
            metadata={
                "parse_mode": "text_only",
                "markdown_skipped_reason": "parse_failed",
            }
        ),
        _settings(),
        markdown_requested=True,
    )

    assert "attempted and failed" in summary.notes[0]


def test_ocr_disabled_is_stated_not_implied():
    """The scanned-PDF-on-a-tenant-without-OCR case: near-empty text that would
    otherwise read as 'this document is nearly blank'."""
    summary = summarize_parse(
        _result(
            text="",
            metadata={
                "pipeline_tier": "fast",
                "ocr_escalation_skipped": "disabled",
                "ocr_recommended_reason": "empty_text",
            },
        ),
        _settings(),
    )

    assert summary.status == "parsed"
    joined = " ".join(summary.notes)
    assert "DOCUMENT_OCR_ENABLED" in joined
    assert "0 characters" in joined


def test_ocr_backend_missing_is_distinguished_from_ocr_off():
    summary = summarize_parse(
        _result(metadata={"ocr_escalation_skipped": "not_registered"}),
        _settings(),
    )

    assert "DOCUMENT_OCR_PROVIDER" in summary.notes[0]


def test_failed_ocr_attempt_is_reported():
    summary = summarize_parse(
        _result(metadata={"pipeline_tier": "fast", "ocr_escalation_failed": "timeout"}),
        _settings(),
    )

    assert "timeout" in summary.notes[0]


def test_failed_parse_is_never_dressed_up_as_content():
    summary = summarize_parse(
        _result(
            text="",
            metadata={"pipeline_tier": "structured", "parse_failed_reason": "oom"},
            processor="pymupdf",
            success=False,
        ),
        _settings(),
    )

    assert summary.status == "failed"
    assert summary.tier == "structured"
    assert summary.processor == "pymupdf"
    assert "oom" in summary.notes[0]
    assert "'structured' tier" in summary.notes[0]


def test_oversize_names_the_cap_and_has_no_tier():
    """The size guard rejects before any tier runs, so there is no tier to name."""
    summary = summarize_parse(
        _result(
            text="",
            metadata={"parse_failed_reason": "oversize"},
            processor="size_guard",
            success=False,
        ),
        _settings(),
    )

    assert summary.status == "failed"
    assert summary.tier is None
    assert "50 MB parse cap" in summary.notes[0]
    assert "DOCUMENT_MAX_PDF_SIZE_MB" in summary.notes[0]


def test_markdown_bookkeeping_is_not_reported_to_a_caller_who_wanted_text():
    """The structured tier is also reached to recover a corrupt text layer, and it
    stamps ``markdown_skipped_reason`` whenever it runs past the page ceiling. A
    caller that asked for text ("auto") got exactly what it asked for -- reporting
    that as a degradation would be a false alarm, and would invite a pointless
    re-request for markdown that hits the same ceiling."""
    summary = summarize_parse(
        _result(
            metadata={
                "pipeline_tier": "structured",
                "parse_mode": "text_only",
                "markdown_skipped_reason": "page_ceiling",
                "page_count": 300,
            },
            processor="pymupdf",
        ),
        _settings(),
    )

    assert summary.status == "parsed"
    assert summary.content_format == "text"
    assert summary.notes == []


def test_pptx_pictures_found_with_no_captioning_attempted():
    """Captioning off/unconfigured: pptx_pictures_captioned is absent entirely
    (not zero) -- that is what distinguishes 'not attempted' from 'attempted,
    none succeeded' (see the next test)."""
    summary = summarize_parse(
        _result(metadata={"parse_mode": "markdown", "pptx_pictures_found": 2}),
        _settings(),
    )

    joined = " ".join(summary.notes)
    assert "2 picture(s)" in joined
    assert "PPTX_CAPTION_IMAGES" in joined


def test_pptx_pictures_partially_captioned_names_the_shortfall():
    summary = summarize_parse(
        _result(
            metadata={
                "parse_mode": "markdown",
                "pptx_pictures_found": 3,
                "pptx_pictures_captioned": 1,
            }
        ),
        _settings(),
    )

    joined = " ".join(summary.notes)
    assert "2 of 3 picture(s)" in joined


def test_pptx_all_pictures_captioned_says_nothing():
    summary = summarize_parse(
        _result(
            metadata={
                "parse_mode": "markdown",
                "pptx_pictures_found": 2,
                "pptx_pictures_captioned": 2,
            }
        ),
        _settings(),
    )

    assert summary.notes == []


def test_pptx_no_pictures_says_nothing():
    summary = summarize_parse(
        _result(metadata={"parse_mode": "markdown", "pptx_pictures_found": 0}),
        _settings(),
    )

    assert summary.notes == []


def test_real_degradations_still_reported_to_a_text_caller():
    """Guard against over-correcting: silencing the markdown note must not silence
    the OCR one, which matters regardless of what the caller asked for."""
    summary = summarize_parse(
        _result(
            metadata={
                "pipeline_tier": "structured",
                "markdown_skipped_reason": "page_ceiling",
                "ocr_escalation_skipped": "disabled",
            }
        ),
        _settings(),
    )

    assert len(summary.notes) == 1
    assert "DOCUMENT_OCR_ENABLED" in summary.notes[0]
