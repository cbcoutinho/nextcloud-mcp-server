"""Tests for the tier-0 shadow-classification wiring in the processor.

Shadow mode = observability only: it emits classification metrics but must never
block or fail indexing, and only applies to PDFs.
"""

from unittest.mock import MagicMock

import pytest

from nextcloud_mcp_server.document_processors.classifier import DocClassification
from nextcloud_mcp_server.vector import processor as proc

pytestmark = pytest.mark.unit


def _classification() -> DocClassification:
    return DocClassification(
        page_count=2,
        sampled_pages=2,
        total_chars=100,
        mean_text_quality=0.9,
        ocr_page_fraction=0.0,
        recommended_tier="fast",
        flags={"image_heavy"},
    )


async def test_shadow_classify_records_metrics(monkeypatch):
    monkeypatch.setattr(proc, "classify_pdf", lambda content: _classification())
    rec = MagicMock()
    monkeypatch.setattr(proc, "record_document_classification", rec)

    await proc._shadow_classify(b"%PDF-1.7", "application/pdf", "f.pdf")

    rec.assert_called_once_with("fast", {"image_heavy"}, 0.9)


async def test_shadow_classify_skips_non_pdf(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(proc, "classify_pdf", called)
    rec = MagicMock()
    monkeypatch.setattr(proc, "record_document_classification", rec)

    await proc._shadow_classify(b"plain", "text/plain", "f.txt")

    called.assert_not_called()
    rec.assert_not_called()


async def test_shadow_classify_swallows_errors(monkeypatch):
    def boom(content):
        raise ValueError("bad pdf")

    monkeypatch.setattr(proc, "classify_pdf", boom)
    rec = MagicMock()
    monkeypatch.setattr(proc, "record_document_classification", rec)

    # Must not raise -- shadow classification is best-effort, off the index path.
    await proc._shadow_classify(b"%PDF-1.7", "application/pdf", "f.pdf")

    rec.assert_not_called()
