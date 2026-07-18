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
    *, ocr: bool, engine: str = "pypdfium2", max_pdf_mb: float = 50.0
) -> SimpleNamespace:
    return SimpleNamespace(
        document_ocr_enabled=ocr,
        document_tier1_engine=engine,
        document_max_pdf_size_mb=max_pdf_mb,
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
