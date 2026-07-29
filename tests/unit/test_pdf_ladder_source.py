"""The file-backed PDF ladder, against real PDFs (Deck #894).

``nc_webdav_read_file`` streams a document to a spool file and parses it from
there, because the API role is not sized to hold a large document in memory.
These tests drive that path end to end with the real built-in tiers -- pypdfium2
(fast) and pymupdf4llm in its isolation subprocess (structured) -- so what they
pin is behaviour, not mocks:

* the source-backed ladder produces the same result as the bytes-backed one, and
* ``prefer_markdown`` reaches the structured tier, but never past the page
  ceiling -- and says so when it stops.
"""

from types import SimpleNamespace

import pymupdf
import pytest

from nextcloud_mcp_server.document_processors import get_registry
from nextcloud_mcp_server.document_processors.pymupdf import PyMuPDFProcessor
from nextcloud_mcp_server.document_processors.source import (
    MemoryDocumentSource,
    SpooledDocumentSource,
)
from nextcloud_mcp_server.utils.document_parser import summarize_parse

pytestmark = pytest.mark.unit


def _digital_pdf(pages: int = 1, body: str = "Ladder source test page") -> bytes:
    """A born-digital PDF: real text layer, no raster."""
    doc = pymupdf.open()
    for n in range(pages):
        page = doc.new_page()
        page.insert_text((72, 720), f"{body} {n + 1}")
    return doc.tobytes()


@pytest.fixture
def spooled(tmp_path):
    """Write bytes to a spool file and hand back a file-backed source."""

    def _make(content: bytes, name: str = "doc.pdf") -> SpooledDocumentSource:
        target = tmp_path / name
        target.write_bytes(content)
        return SpooledDocumentSource(
            spool_path=target,
            content_type="application/pdf",
            filename=name,
            _size=len(content),
        )

    return _make


@pytest.fixture
def settings(mocker):
    """Registry settings, defaulted to a stock deployment."""

    def _install(**overrides):
        values = {
            "document_classify_enabled": True,
            "document_tier1_engine": "pypdfium2",
            "document_ocr_enabled": False,
            "document_ocr_detect_scanned": True,
            "document_ocr_min_text_quality": 0.5,
            "document_ocr_page_fraction": 0.5,
            "document_ocr_min_page_chars": 16,
            "document_glyph_corruption_ratio": 0.02,
            "document_max_pdf_size_mb": 50.0,
            "document_markdown_max_pages": 150,
            "document_pdf_graphics_limit": 1000,
            "document_parse_timeout_seconds": 120.0,
            "document_parse_mem_limit_mb": 1536,
            "document_parse_page_window": 100,
            "document_parse_process_slots": 2,
        }
        values.update(overrides)
        stub = SimpleNamespace(**values)
        for module in (
            "nextcloud_mcp_server.document_processors.registry",
            "nextcloud_mcp_server.document_processors.pymupdf",
            "nextcloud_mcp_server.document_processors.pypdfium2_fast",
        ):
            mocker.patch(f"{module}.get_settings", return_value=stub)
        return stub

    return _install


async def test_source_path_matches_the_bytes_path(spooled, settings):
    """Opening by path must not change what the ladder produces."""
    settings()
    content = _digital_pdf()
    registry = get_registry()

    from_bytes = await registry.process(content, "application/pdf", "doc.pdf")
    from_source = await registry.process_source(spooled(content))

    assert from_source.success is True
    assert from_source.text.strip() == from_bytes.text.strip()
    assert from_source.metadata["pipeline_tier"] == from_bytes.metadata["pipeline_tier"]


async def test_default_read_stops_at_the_fast_tier(spooled, settings):
    """A good text layer needs nothing more, and is labelled plain text."""
    settings()

    result = await get_registry().process_source(spooled(_digital_pdf()))

    assert result.success is True
    assert result.metadata["pipeline_tier"] == "fast"
    assert result.metadata["parse_mode"] == "text_only"
    assert "Ladder source test page" in result.text


async def test_prefer_markdown_promotes_to_the_structured_tier(spooled, settings):
    settings()

    result = await get_registry().process_source(
        spooled(_digital_pdf()), options={"prefer_markdown": True}
    )

    assert result.success is True
    assert result.metadata["pipeline_tier"] == "structured"
    assert result.metadata["parse_mode"] == "markdown"
    assert "Ladder source test page" in result.text


async def test_prefer_markdown_stops_at_the_page_ceiling_and_says_so(spooled, settings):
    """Past the ceiling the structured tier would return raw text anyway, so the
    promotion is skipped -- and the reason is recorded rather than implied."""
    settings(document_markdown_max_pages=1)

    result = await get_registry().process_source(
        spooled(_digital_pdf(pages=3)), options={"prefer_markdown": True}
    )

    assert result.success is True
    assert result.metadata["pipeline_tier"] == "fast"
    assert result.metadata["markdown_skipped_reason"] == "page_ceiling"
    assert result.metadata["page_count"] == 3


async def test_oversize_is_rejected_from_the_size_without_reading_the_file(
    spooled, settings
):
    """The guard runs off ``source.size``, so an over-cap document is never read."""
    settings(document_max_pdf_size_mb=0.000001)

    result = await get_registry().process_source(spooled(_digital_pdf()))

    assert result.success is False
    assert result.processor == "size_guard"
    assert result.metadata["parse_failed_reason"] == "oversize"


async def test_auto_mode_escalation_past_the_ceiling_reports_nothing_to_the_caller(
    spooled, settings
):
    """A read that never asked for markdown must not be told it was denied any.

    The structured tier is reached two ways: to honour a markdown request, and --
    as here -- to recover a text layer the classifier distrusts. It stamps
    ``markdown_skipped_reason`` whenever it runs past the page ceiling either way,
    so the *fact* is recorded; what must not happen is surfacing it as a
    degradation to a caller who asked for text and got text.

    Drives the real pymupdf tier: ``min_text_quality=1.1`` is unsatisfiable, so
    every page reads as low-confidence and the ladder escalates fast->structured
    with non-empty text, exactly as a glyph-corrupt document would.
    """
    stub = settings(document_markdown_max_pages=1, document_ocr_min_text_quality=1.1)

    result = await get_registry().process_source(spooled(_digital_pdf(pages=3)))

    # Escalated for recovery, not for markdown, and past the ceiling.
    assert result.metadata["pipeline_tier"] == "structured"
    assert result.metadata["parse_mode"] == "text_only"
    assert result.metadata["markdown_skipped_reason"] == "page_ceiling"

    # parse_document="auto" -> the caller hears nothing about markdown. (It does
    # hear that OCR was unavailable: the same distrusted text layer is a real
    # degradation of what it DID ask for, so that note must survive.)
    auto = summarize_parse(result, stub)
    assert not any("markdown" in note.lower() for note in auto.notes)
    assert any("DOCUMENT_OCR_ENABLED" in note for note in auto.notes)

    # parse_document="markdown" -> the same result does explain itself.
    requested = summarize_parse(result, stub, markdown_requested=True)
    assert any("DOCUMENT_MARKDOWN_MAX_PAGES" in note for note in requested.notes)


async def test_in_memory_source_still_works(settings):
    """Notes/deck attachments stay in memory; they must not regress to disk."""
    settings()

    result = await get_registry().process_source(
        MemoryDocumentSource(_digital_pdf(), "application/pdf", "doc.pdf")
    )

    assert result.success is True
    assert result.metadata["pipeline_tier"] == "fast"


async def test_unreadable_file_fails_gracefully_instead_of_raising(spooled, settings):
    """A file named ``.pdf`` whose bytes are not a document must not raise.

    Nextcloud derives the mime type from the extension, so a zero-filled recovery
    artifact named ``.pdf`` is served as application/pdf and routed to the PDF
    ladder. The structured tier used to raise ``ProcessorError`` here, which skips
    ``vector.processor``'s escalate-or-dead-letter path entirely: the failure was
    labelled ``other``, the document was deliberately NOT marked failed, and the
    scanner re-queued it forever -- re-downloading the whole file every cycle.

    Returning ``success=False`` puts it on the terminal path instead.

    This case is unreachable-by-content: a pure zero-fill has nothing for MuPDF's
    xref repair to reconstruct, so it fails even with ``filetype="pdf"`` -- which
    is why the graceful-failure branch is needed on top of the type hint that
    ``test_pdf_behind_a_junk_prefix_is_still_read`` covers.
    """
    settings()

    result = await PyMuPDFProcessor().process_source(
        spooled(b"\0" * 4096, name="nc-ingest-zeroes.bin")
    )

    assert result.success is False
    assert result.metadata["parse_failed_reason"] == "unreadable"
    assert "not a readable PDF" in (result.error or "")


async def test_valid_pdf_parses_from_a_bin_suffixed_spool(spooled, settings):
    """The spool's ``.bin`` suffix must stay irrelevant to a real PDF."""
    settings()

    result = await PyMuPDFProcessor().process_source(
        spooled(_digital_pdf(), name="nc-ingest-valid.bin")
    )

    assert result.success is True
    assert "Ladder source test page" in result.text


async def test_pdf_behind_a_junk_prefix_is_still_read(spooled, settings):
    """A PDF whose header sits past MuPDF's sniffing window must still parse.

    This is the case that makes ``filetype="pdf"`` load-bearing rather than
    cosmetic. Handler selection from the ``.bin`` spool suffix finds no handler, so
    MuPDF falls back to sniffing a prefix only; with megabytes of junk in front of
    ``%PDF-`` -- what recovery tools emit -- the sniff misses it and the document
    is rejected outright, even though the PDF handler's xref repair reconstructs it
    fine. Naming the type skips the guess and lets repair run.

    Uses 8 MB of padding to clear the sniffing window by a wide margin, matching
    the real document that surfaced this (content began ~7 MB in).
    """
    settings()

    result = await PyMuPDFProcessor().process_source(
        spooled(b"\0" * (8 * 1024 * 1024) + _digital_pdf(), name="nc-ingest-junk.bin")
    )

    assert result.success is True
    assert "Ladder source test page" in result.text
