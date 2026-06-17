"""Unit tests for the processor's terminal-parse-failure dead-lettering.

When a PDF parse fails permanently (isolated-worker timeout/OOM) and the failing
tier has NO higher escalation tier available (e.g. ``structured`` with OCR off),
``_index_document`` records a durable, content-addressed dead-letter marker
instead of the per-user ``status="failed"`` placeholder mark — the latter could
not stop the multi-user re-queue loop. A failure that still has a higher tier
keeps the legacy per-user mark.

The real ``ProcessorRegistry`` singleton is used so the terminal decision
(``next_available_tier``) is exercised faithfully; only the parse itself, the
content fetch, and the Qdrant side-effects are mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.document_processors.base import ProcessingResult
from nextcloud_mcp_server.vector import processor
from nextcloud_mcp_server.vector.scanner import DocumentTask

pytestmark = pytest.mark.unit


def _settings(*, ocr_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        document_ocr_enabled=ocr_enabled,
        document_tier1_engine="pypdfium2",
        get_collection_name=lambda: "c",
    )


def _file_task() -> DocumentTask:
    return DocumentTask(
        user_id="Demo-User",
        doc_id="520189",
        doc_type="file",
        operation="index",
        modified_at=0,
        file_path="/Plans/big.pdf",
        etag="etag-1",
    )


def _nc_client() -> MagicMock:
    # MagicMock (typed Any) keeps the pre-commit ty-check happy where the real
    # signature wants a NextcloudClient -- matching the other processor tests.
    nc = MagicMock()
    nc.webdav.read_file = AsyncMock(return_value=(b"%PDF-1.4", "application/pdf"))
    return nc


def _patch_common(mocker, *, ocr_enabled: bool):
    """Patch the shared seams; returns the spies for assertions."""
    mocker.patch.object(
        processor, "get_settings", lambda: _settings(ocr_enabled=ocr_enabled)
    )
    # Never a tenant-wide dedup hit (file was never indexed).
    mocker.patch.object(
        processor, "claim_existing_index", AsyncMock(return_value=False)
    )
    spies = SimpleNamespace(
        mark=mocker.patch.object(processor, "mark_dead_letter", AsyncMock()),
        dead_metric=mocker.patch.object(processor, "record_document_dead_lettered"),
        delete_ph=mocker.patch.object(
            processor, "delete_placeholder_point", AsyncMock()
        ),
        update_ph=mocker.patch.object(
            processor, "update_placeholder_status", AsyncMock()
        ),
    )
    return spies


async def test_terminal_failure_dead_letters(mocker):
    """structured tier fails + OCR off (no higher tier) -> dead-letter, not mark."""
    spies = _patch_common(mocker, ocr_enabled=False)
    # The per-tier worker runs the structured tier and the parse times out.
    mocker.patch.object(
        processor,
        "_parse_pdf_tier",
        AsyncMock(
            return_value=ProcessingResult(
                text="",
                metadata={
                    "parse_failed_reason": "timeout",
                    "pipeline_tier": "structured",
                },
                processor="pymupdf",
                success=False,
                error="isolated parse failed (timeout)",
            )
        ),
    )

    result = await processor._index_document(
        _file_task(), _nc_client(), MagicMock(), tier="structured"
    )

    assert result is False
    spies.mark.assert_awaited_once()
    # Marker is content-addressed with this etag + the OCR-off tiers signature.
    args = spies.mark.await_args.args
    assert args[0] == "520189" and args[1] == "file"
    assert args[2] == "etag-1"  # etag
    assert "ocr=0" in args[3]  # tiers_sig
    assert args[4] == "timeout"  # reason
    spies.dead_metric.assert_called_once_with("timeout")
    spies.delete_ph.assert_awaited_once()  # volatile placeholder dropped
    spies.update_ph.assert_not_awaited()  # NOT the legacy per-user failed mark


async def test_non_terminal_failure_keeps_legacy_mark(mocker):
    """fast tier fails while structured is still available -> legacy failed mark."""
    spies = _patch_common(mocker, ocr_enabled=False)
    mocker.patch.object(
        processor,
        "_parse_pdf_tier",
        AsyncMock(
            return_value=ProcessingResult(
                text="",
                metadata={"parse_failed_reason": "error", "pipeline_tier": "fast"},
                processor="pypdfium2",
                success=False,
                error="isolated parse failed (error)",
            )
        ),
    )

    result = await processor._index_document(
        _file_task(), _nc_client(), MagicMock(), tier="fast"
    )

    assert result is False
    spies.update_ph.assert_awaited_once()  # legacy per-user failed mark
    spies.mark.assert_not_awaited()  # NOT dead-lettered (structured can still run)
    spies.dead_metric.assert_not_called()


async def test_oversize_failure_dead_letters_regardless_of_tier(mocker):
    """An oversize PDF is terminal at any tier (no tier can parse it) -> dead-letter
    even though a higher tier (structured) is nominally available above 'fast'."""
    spies = _patch_common(mocker, ocr_enabled=False)
    mocker.patch.object(
        processor,
        "_parse_pdf_tier",
        AsyncMock(
            return_value=ProcessingResult(
                text="",
                metadata={"parse_failed_reason": "oversize"},
                processor="size_guard",
                success=False,
                error="PDF exceeds size cap",
            )
        ),
    )

    result = await processor._index_document(
        _file_task(), _nc_client(), MagicMock(), tier="fast"
    )

    assert result is False
    spies.mark.assert_awaited_once()
    assert spies.mark.await_args.args[4] == "oversize"  # reason
    spies.dead_metric.assert_called_once_with("oversize")
    spies.update_ph.assert_not_awaited()


async def test_terminal_failure_without_etag_uses_legacy_mark(mocker):
    """A terminal failure with no etag can't be content-addressed, so fall back to
    the legacy per-user placeholder mark instead of writing an unmatchable marker."""
    spies = _patch_common(mocker, ocr_enabled=False)
    mocker.patch.object(
        processor,
        "_parse_pdf_tier",
        AsyncMock(
            return_value=ProcessingResult(
                text="",
                metadata={
                    "parse_failed_reason": "timeout",
                    "pipeline_tier": "structured",
                },
                processor="pymupdf",
                success=False,
                error="isolated parse failed (timeout)",
            )
        ),
    )
    task = _file_task()
    task.etag = None  # no content key

    result = await processor._index_document(
        task, _nc_client(), MagicMock(), tier="structured"
    )

    assert result is False
    spies.mark.assert_not_awaited()  # no unmatchable marker written
    spies.update_ph.assert_awaited_once()  # legacy fallback


async def test_delete_clears_dead_letter_marker(mocker):
    """Deleting a file must also drop its dead-letter marker, else a
    dead-lettered-then-deleted file leaves an orphan accumulating in Qdrant
    (release_document_for_user's filter misses the user-agnostic marker)."""
    mocker.patch.object(processor, "get_qdrant_client", AsyncMock())
    mocker.patch.object(processor, "release_document_for_user", AsyncMock())
    clear = mocker.patch.object(processor, "clear_dead_letter", AsyncMock())

    task = DocumentTask(
        user_id="Demo-User",
        doc_id="520189",
        doc_type="file",
        operation="delete",
        modified_at=0,
        file_path="/Plans/big.pdf",
        etag="etag-1",
    )
    await processor.process_document(task, MagicMock(), max_retries=1)

    clear.assert_awaited_once_with("520189", "file")
