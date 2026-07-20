"""Unit tests for the escalation-tier signature used by dead-letter keying.

``escalation_tiers_signature`` fingerprints the runtime escalation config so a
dead-lettered document becomes retryable when a new tier appears (e.g. an
operator enables OCR). It must be settings-derived (role-independent) and must
change when OCR is toggled.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nextcloud_mcp_server.document_processors.escalation import (
    escalation_tiers_signature,
)

pytestmark = pytest.mark.unit


def _settings(
    *,
    ocr: bool,
    engine: str = "pypdfium2",
    max_pdf_mb: float = 50.0,
    markdown_max_pages: int = 150,
) -> SimpleNamespace:
    return SimpleNamespace(
        document_ocr_enabled=ocr,
        document_tier1_engine=engine,
        document_max_pdf_size_mb=max_pdf_mb,
        document_markdown_max_pages=markdown_max_pages,
    )


def test_signature_is_stable_for_same_config() -> None:
    assert escalation_tiers_signature(
        _settings(ocr=False)
    ) == escalation_tiers_signature(_settings(ocr=False))


def test_enabling_ocr_changes_signature() -> None:
    # Enabling OCR adds an escalation tier -> previously dead-lettered docs retry.
    assert escalation_tiers_signature(
        _settings(ocr=False)
    ) != escalation_tiers_signature(_settings(ocr=True))


def test_tier1_engine_change_changes_signature() -> None:
    assert escalation_tiers_signature(
        _settings(ocr=False, engine="pypdfium2")
    ) != escalation_tiers_signature(_settings(ocr=False, engine="pymupdf"))


def test_raising_size_cap_changes_signature() -> None:
    # An oversize PDF is always-terminal, so without the cap in the signature it
    # stays dead-lettered until its etag changes -- which for an archive of
    # scanned documents is never. Raising the cap must re-drive them.
    assert escalation_tiers_signature(
        _settings(ocr=False, max_pdf_mb=50.0)
    ) != escalation_tiers_signature(_settings(ocr=False, max_pdf_mb=2000.0))


def test_size_cap_int_and_float_fingerprint_identically() -> None:
    # ":g" formatting keeps 50 and 50.0 equal, so a float-repr change cannot
    # spuriously invalidate every dead letter on the tenant.
    assert escalation_tiers_signature(
        _settings(ocr=False, max_pdf_mb=50)
    ) == escalation_tiers_signature(_settings(ocr=False, max_pdf_mb=50.0))


def test_disabling_size_cap_changes_signature() -> None:
    assert escalation_tiers_signature(
        _settings(ocr=False, max_pdf_mb=50.0)
    ) != escalation_tiers_signature(_settings(ocr=False, max_pdf_mb=0))


def test_markdown_page_gate_change_changes_signature() -> None:
    # A structured-tier timeout is terminal, so lowering the page ceiling (which
    # sends the document down the raw-text path and lets it succeed) must
    # re-drive documents dead-lettered under the old value -- for a scanned
    # archive the etag never changes, so nothing else would.
    assert escalation_tiers_signature(
        _settings(ocr=False, markdown_max_pages=150)
    ) != escalation_tiers_signature(_settings(ocr=False, markdown_max_pages=50))


def test_disabling_markdown_changes_signature() -> None:
    # 0 == "never run to_markdown" is a distinct configuration, not a no-op.
    assert escalation_tiers_signature(
        _settings(ocr=False, markdown_max_pages=150)
    ) != escalation_tiers_signature(_settings(ocr=False, markdown_max_pages=0))
