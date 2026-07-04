"""Unit tests for the processor's terminal-parse-failure dead-lettering.

When a PDF parse fails permanently (isolated-worker timeout/OOM) and the failing
tier has NO higher escalation tier available (e.g. ``structured`` with OCR off),
``_index_document`` records a durable, content-addressed dead-letter marker
instead of the per-user ``status="failed"`` placeholder mark — the latter could
not stop the multi-user re-queue loop. A failure that still has a higher tier
available now ESCALATES to it (#399) — e.g. a structured-tier timeout hops to OCR
— rather than being marked failed and re-queued into the same tier forever.

The real ``ProcessorRegistry`` singleton is used so the terminal decision
(``next_available_tier``) is exercised faithfully; only the parse itself, the
content fetch, and the Qdrant side-effects are mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.document_processors.base import ProcessingResult
from nextcloud_mcp_server.document_processors.escalation import (
    BatchPending,
    EscalateError,
)
from nextcloud_mcp_server.vector import processor
from nextcloud_mcp_server.vector.scanner import DocumentTask

pytestmark = pytest.mark.unit


def _settings(*, ocr_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        document_ocr_enabled=ocr_enabled,
        # A real Settings always carries this; "sync" keeps the Deck #516
        # skip-redownload guard in process_document inert on these non-batch paths.
        document_ocr_mode="sync",
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
        escalation=mocker.patch.object(processor, "record_document_escalation"),
        parse_failed=mocker.patch.object(processor, "record_document_parse_failed"),
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
    # Terminal failure still counts as a parse failure and does not escalate.
    spies.parse_failed.assert_called_once_with("timeout")
    spies.escalation.assert_not_called()


async def test_non_terminal_failure_escalates_to_next_tier(mocker):
    """fast tier fails while structured is still available -> escalate (#399).

    A hard parse failure is no longer dropped when a higher tier can still run:
    it hops to the next tier instead of being marked failed and re-queued onto
    the same failing tier forever.
    """
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

    with pytest.raises(EscalateError) as ei:
        await processor._index_document(
            _file_task(), _nc_client(), MagicMock(), tier="fast"
        )

    assert ei.value.from_tier == "fast"
    assert ei.value.to_tier == "structured"
    assert ei.value.reason == "error"
    spies.escalation.assert_called_once_with("fast", "structured", "error")
    # An escalation is NOT a parse failure: it must not inflate the failure panel
    # nor mark/dead-letter the doc.
    spies.parse_failed.assert_not_called()
    spies.update_ph.assert_not_awaited()
    spies.mark.assert_not_awaited()
    spies.dead_metric.assert_not_called()


async def test_structured_timeout_escalates_to_ocr_when_enabled(mocker):
    """The 406-105 case: structured pymupdf timeout + OCR enabled -> hop to OCR.

    Previously this marked the doc 'failed' and the scanner re-queued it into the
    structured tier forever (the retry loop). Now it escalates to OCR, where surya
    rasterizes + reads the rendered glyphs.
    """
    spies = _patch_common(mocker, ocr_enabled=True)
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

    with pytest.raises(EscalateError) as ei:
        await processor._index_document(
            _file_task(), _nc_client(), MagicMock(), tier="structured"
        )

    assert (ei.value.from_tier, ei.value.to_tier, ei.value.reason) == (
        "structured",
        "ocr",
        "timeout",
    )
    spies.escalation.assert_called_once_with("structured", "ocr", "timeout")
    spies.parse_failed.assert_not_called()
    spies.mark.assert_not_awaited()
    spies.update_ph.assert_not_awaited()


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


async def test_batch_ocr_pending_defers_before_download(mocker):
    """Deck #518: a still-pending batch OCR job defers via BatchPending BEFORE the
    WebDAV fetch — the whole point of the change (no re-download on poll retries).

    Guards the call ordering at the integration point: ``poll_pending_batch_ocr``
    returning a non-None interval must short-circuit to ``BatchPending`` without ever
    reaching ``nc_client.webdav.read_file``."""
    settings = _settings(ocr_enabled=True)
    settings.document_ocr_mode = "batch"  # activate the pre-read poll fast-path
    mocker.patch.object(processor, "get_settings", lambda: settings)
    mocker.patch.object(
        processor, "claim_existing_index", AsyncMock(return_value=False)
    )
    # The poll fast-path reports the job is still pending -> defer for 120s.
    poll = mocker.patch(
        "nextcloud_mcp_server.document_processors.ocr.poll_pending_batch_ocr",
        AsyncMock(return_value=120),
    )
    nc = _nc_client()

    with pytest.raises(BatchPending) as ei:
        await processor._index_document(_file_task(), nc, MagicMock(), tier="ocr")

    assert ei.value.retry_in == 120
    poll.assert_awaited_once()
    nc.webdav.read_file.assert_not_awaited()  # the win: no re-download on a poll


async def test_batch_ocr_no_pending_job_still_downloads(mocker):
    """The inverse guard: when poll_pending_batch_ocr returns None (no in-flight
    job), _index_document falls through to the normal fetch (read_file IS called)."""
    settings = _settings(ocr_enabled=True)
    settings.document_ocr_mode = "batch"
    mocker.patch.object(processor, "get_settings", lambda: settings)
    mocker.patch.object(
        processor, "claim_existing_index", AsyncMock(return_value=False)
    )
    mocker.patch(
        "nextcloud_mcp_server.document_processors.ocr.poll_pending_batch_ocr",
        AsyncMock(return_value=None),
    )
    # Stop after the fetch so we only assert the download happened, not full indexing.
    mocker.patch.object(
        processor,
        "_parse_pdf_tier",
        AsyncMock(side_effect=BatchPending(retry_in=99)),
    )
    nc = _nc_client()

    with pytest.raises(BatchPending):
        await processor._index_document(_file_task(), nc, MagicMock(), tier="ocr")

    nc.webdav.read_file.assert_awaited_once()  # no in-flight job -> fetched normally
