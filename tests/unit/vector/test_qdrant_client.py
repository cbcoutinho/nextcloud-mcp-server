"""Unit tests for Qdrant payload-index helpers and doc_id backfill.

These cover the startup-time migrations added to ``vector/qdrant_client.py``
after production HTTP 400 errors revealed that:

1. The collection had no payload index for ``doc_id``, so any
   ``FieldCondition(key="doc_id", ...)`` filter failed at the Qdrant layer.
2. Producers wrote a mix of ``int`` and ``str`` values for ``doc_id``, so a
   single keyword index could not have covered both kinds even if it had
   existed.

The fix has three coordinated parts; this module covers the two helpers that
run at startup. Producer-side normalization is exercised by the existing
scanner tests.
"""

from types import SimpleNamespace
from unittest.mock import call

import httpx
import pytest
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import PayloadSchemaType

from nextcloud_mcp_server.vector.qdrant_client import (
    _KEYWORD_PAYLOAD_FIELDS,
    _backfill_doc_id_to_string,
    _ensure_keyword_payload_indexes,
    _has_int_doc_id_sample,
)


def _make_unexpected(status_code: int, body: bytes) -> UnexpectedResponse:
    """Build a real UnexpectedResponse for raise_for_status-style branches."""
    return UnexpectedResponse(
        status_code=status_code,
        reason_phrase="Bad Request",
        content=body,
        headers=httpx.Headers(),
    )


def _record(point_id: int | str, doc_id: int | str | None) -> SimpleNamespace:
    """Stand-in for qdrant_client.http.models.Record.

    Tests don't need full Pydantic validation — only ``id`` and ``payload``
    are read by the helpers under test.
    """
    payload: dict | None = {"doc_id": doc_id} if doc_id is not None else None
    return SimpleNamespace(id=point_id, payload=payload)


# ---------------------------------------------------------------------------
# _ensure_keyword_payload_indexes
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_ensure_keyword_payload_indexes_creates_each_field(mocker):
    """Happy path: every field in _KEYWORD_PAYLOAD_FIELDS gets a KEYWORD index."""
    client = mocker.AsyncMock()

    await _ensure_keyword_payload_indexes(client, "test-collection")

    assert client.create_payload_index.await_count == len(_KEYWORD_PAYLOAD_FIELDS)
    expected_calls = [
        call(
            collection_name="test-collection",
            field_name=field,
            field_schema=PayloadSchemaType.KEYWORD,
            wait=True,
        )
        for field in _KEYWORD_PAYLOAD_FIELDS
    ]
    client.create_payload_index.assert_has_awaits(expected_calls, any_order=False)


@pytest.mark.unit
async def test_ensure_keyword_payload_indexes_swallows_already_exists(mocker, caplog):
    """Idempotent: 'already exists' 400 is logged at debug, not raised."""
    client = mocker.AsyncMock()
    # First call succeeds, second raises "already exists", third succeeds —
    # exercises the per-field exception handling.
    client.create_payload_index.side_effect = [
        None,
        _make_unexpected(
            400, b'{"status":{"error":"Index for \\"user_id\\" already exists"}}'
        ),
        None,
    ]

    with caplog.at_level("DEBUG", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_keyword_payload_indexes(client, "test-collection")

    assert client.create_payload_index.await_count == len(_KEYWORD_PAYLOAD_FIELDS)
    # The "already exists" branch logs at DEBUG; nothing reaches WARNING.
    assert not any(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.unit
async def test_ensure_keyword_payload_indexes_logs_unrelated_400_as_warning(
    mocker, caplog
):
    """Schema conflicts and other 400s are surfaced as warnings, not silenced."""
    client = mocker.AsyncMock()
    client.create_payload_index.side_effect = [
        _make_unexpected(
            400,
            b'{"status":{"error":"field \\"doc_id\\" indexed with different schema"}}',
        ),
        None,
        None,
    ]

    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_keyword_payload_indexes(client, "test-collection")

    # Loop continued past the failing field; all three were attempted.
    assert client.create_payload_index.await_count == len(_KEYWORD_PAYLOAD_FIELDS)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "different schema" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# _has_int_doc_id_sample
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_has_int_doc_id_sample_returns_true_when_int_present(mocker):
    """Sample finds an int — caller should run the full backfill."""
    client = mocker.AsyncMock()
    client.scroll.return_value = (
        [_record(1, "abc"), _record(2, 42), _record(3, "xyz")],
        None,
    )

    assert await _has_int_doc_id_sample(client, "c") is True
    client.scroll.assert_awaited_once()


@pytest.mark.unit
async def test_has_int_doc_id_sample_returns_false_when_all_str(mocker):
    """Sample is clean — caller should skip the full scroll."""
    client = mocker.AsyncMock()
    client.scroll.return_value = (
        [_record(1, "abc"), _record(2, "def")],
        None,
    )

    assert await _has_int_doc_id_sample(client, "c") is False


@pytest.mark.unit
async def test_has_int_doc_id_sample_handles_empty_collection(mocker):
    """Empty collection — nothing to backfill, return False."""
    client = mocker.AsyncMock()
    client.scroll.return_value = ([], None)

    assert await _has_int_doc_id_sample(client, "c") is False


@pytest.mark.unit
async def test_has_int_doc_id_sample_ignores_missing_payload(mocker):
    """Records with no payload don't count as int doc_ids."""
    client = mocker.AsyncMock()
    client.scroll.return_value = (
        [_record(1, None), _record(2, "abc")],
        None,
    )

    assert await _has_int_doc_id_sample(client, "c") is False


# ---------------------------------------------------------------------------
# _backfill_doc_id_to_string
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_backfill_skips_when_sample_is_clean(mocker):
    """Short-circuit: clean sample → no full scroll, no set_payload calls."""
    client = mocker.AsyncMock()
    # Sample call returns only str payloads → backfill should not proceed.
    client.scroll.return_value = ([_record(1, "abc"), _record(2, "def")], None)

    await _backfill_doc_id_to_string(client, "test-collection")

    # Exactly one scroll (the sample) and zero rewrites.
    assert client.scroll.await_count == 1
    client.set_payload.assert_not_awaited()


@pytest.mark.unit
async def test_backfill_rewrites_int_doc_ids_to_str(mocker):
    """Mixed int/str payload across two scroll pages: only ints get rewritten."""
    client = mocker.AsyncMock()
    # Three scroll calls:
    #   1. sample → finds an int, triggers the full pass
    #   2. first batch of full scroll → mixed int/str
    #   3. second batch → all str, with next_offset=None to terminate
    client.scroll.side_effect = [
        ([_record(1, 100), _record(2, "abc")], None),  # sample
        ([_record(1, 100), _record(2, "abc")], "next-offset-123"),  # batch 1
        ([_record(3, 200), _record(4, "def")], None),  # batch 2 (terminal)
    ]

    await _backfill_doc_id_to_string(client, "test-collection")

    # Two rewrites: point 1 (int 100) in batch 1, point 3 (int 200) in batch 2.
    assert client.set_payload.await_count == 2
    client.set_payload.assert_any_await(
        collection_name="test-collection",
        payload={"doc_id": "100"},
        points=[1],
        wait=False,
    )
    client.set_payload.assert_any_await(
        collection_name="test-collection",
        payload={"doc_id": "200"},
        points=[3],
        wait=False,
    )


@pytest.mark.unit
async def test_backfill_emits_completion_log(mocker, caplog):
    """Backfill logs final rewritten/scanned counts at INFO."""
    client = mocker.AsyncMock()
    client.scroll.side_effect = [
        ([_record(1, 7)], None),  # sample triggers full pass
        ([_record(1, 7), _record(2, "x")], None),  # single batch, terminal
    ]

    with caplog.at_level("INFO", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _backfill_doc_id_to_string(client, "test-collection")

    completion_logs = [
        r.getMessage() for r in caplog.records if "backfill complete" in r.getMessage()
    ]
    assert completion_logs, "expected an INFO log line for backfill completion"
    msg = completion_logs[0]
    assert "1/2" in msg, f"expected '1/2' rewritten/scanned in {msg!r}"


@pytest.mark.unit
async def test_backfill_handles_none_payload(mocker):
    """A point with payload=None is skipped without crashing."""
    client = mocker.AsyncMock()
    client.scroll.side_effect = [
        ([_record(1, 99)], None),  # sample triggers full pass
        ([_record(1, None), _record(2, 99)], None),  # batch with one None payload
    ]

    await _backfill_doc_id_to_string(client, "test-collection")

    # Only the int doc_id at point 2 was rewritten; the None-payload point was skipped.
    assert client.set_payload.await_count == 1
    client.set_payload.assert_awaited_with(
        collection_name="test-collection",
        payload={"doc_id": "99"},
        points=[2],
        wait=False,
    )
