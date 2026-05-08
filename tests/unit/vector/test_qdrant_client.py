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
    _DOC_ID_BACKFILL_SENTINEL_ID,
    _KEYWORD_PAYLOAD_FIELDS,
    _backfill_doc_id_to_string,
    _ensure_keyword_payload_indexes,
)


def _empty_collection_info() -> SimpleNamespace:
    """Stand-in for a CollectionInfo with no payload indexes yet.

    Tests for _ensure_keyword_payload_indexes only read ``payload_schema``
    off the result. None / empty dict both signal "no indexes" — use {}
    here to match the production-code default.
    """
    return SimpleNamespace(payload_schema={})


def _backfill_dimension() -> int:
    """Vector dimension for sentinel writes in backfill tests.

    Any positive int is fine — the sentinel point is never read by the
    test bodies, only the upsert call site is asserted.
    """
    return 4


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
    client.get_collection.return_value = _empty_collection_info()

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
async def test_ensure_keyword_payload_indexes_skips_fields_already_indexed(
    mocker, caplog
):
    """Routine restart path: existing payload indexes are silently skipped.

    Without the pre-fetch, every restart logs `Created KEYWORD payload
    index on '<field>'` for every field — noise that hides genuinely
    interesting first-time-creation lines. With the pre-fetch, no log
    fires and no Qdrant write round-trip happens for already-indexed
    fields.
    """
    client = mocker.AsyncMock()
    client.get_collection.return_value = SimpleNamespace(
        payload_schema={"doc_id": object()}
    )

    with caplog.at_level("INFO", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_keyword_payload_indexes(client, "test-collection")

    # Only the two missing fields are created.
    assert client.create_payload_index.await_count == 2
    created_fields = {
        c.kwargs["field_name"] for c in client.create_payload_index.await_args_list
    }
    assert created_fields == {"user_id", "doc_type"}
    # No INFO log fires for the already-indexed field.
    info_messages = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert not any("doc_id" in m for m in info_messages), info_messages


@pytest.mark.unit
async def test_ensure_keyword_payload_indexes_logs_400_as_warning(mocker, caplog):
    """Any 400 from create_payload_index is logged at WARNING and skipped.

    Real Qdrant returns 200 when the index already exists with a matching
    schema, so 400s indicate a genuine problem (e.g., schema conflict on a
    pre-existing index). The loop continues past the failure so the
    remaining fields still get indexed.
    """
    client = mocker.AsyncMock()
    client.get_collection.return_value = _empty_collection_info()
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


@pytest.mark.unit
async def test_ensure_keyword_payload_indexes_logs_non_400_as_error(mocker, caplog):
    """A non-400 status from create_payload_index escalates to ERROR.

    A 5xx response (e.g., Qdrant temporarily unavailable) should not be
    silently downgraded to a warning the way a 400 schema-conflict is.
    The loop still continues so the remaining fields get attempted.
    """
    client = mocker.AsyncMock()
    client.get_collection.return_value = _empty_collection_info()
    client.create_payload_index.side_effect = [
        _make_unexpected(500, b'{"status":{"error":"internal server error"}}'),
        None,
        None,
    ]

    with caplog.at_level("ERROR", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_keyword_payload_indexes(client, "test-collection")

    assert client.create_payload_index.await_count == len(_KEYWORD_PAYLOAD_FIELDS)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert "500" in msg
    assert "internal server error" in msg


# ---------------------------------------------------------------------------
# _backfill_doc_id_to_string
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_backfill_clean_collection_makes_no_writes(mocker, caplog):
    """A collection with only str doc_ids triggers zero set_payload calls.

    Verifies the no-write path: scroll runs, no payloads need rewriting,
    and a sentinel is written so subsequent restarts can short-circuit.
    """
    client = mocker.AsyncMock()
    client.retrieve.return_value = []  # No sentinel — backfill must run
    client.scroll.return_value = (
        [_record(1, "abc"), _record(2, "def")],
        None,
    )

    with caplog.at_level("INFO", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _backfill_doc_id_to_string(
            client, "test-collection", _backfill_dimension()
        )

    client.set_payload.assert_not_awaited()
    completion_logs = [
        r.getMessage() for r in caplog.records if "backfill complete" in r.getMessage()
    ]
    assert completion_logs, "expected an INFO log line for backfill completion"
    assert "0/2" in completion_logs[0]


@pytest.mark.unit
async def test_backfill_skips_when_sentinel_present(mocker, caplog):
    """If the sentinel exists, retrieve() returns it and the scroll is skipped.

    This is the routine-restart fast path: the migration already ran on a
    previous start, so we avoid the O(N) scroll entirely.
    """
    client = mocker.AsyncMock()
    client.retrieve.return_value = [SimpleNamespace(id=_DOC_ID_BACKFILL_SENTINEL_ID)]

    with caplog.at_level("DEBUG", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _backfill_doc_id_to_string(
            client, "test-collection", _backfill_dimension()
        )

    client.scroll.assert_not_awaited()
    client.set_payload.assert_not_awaited()
    client.upsert.assert_not_awaited()
    debug_msgs = [r.getMessage() for r in caplog.records if r.levelname == "DEBUG"]
    assert any("sentinel" in m and "skipping" in m for m in debug_msgs), debug_msgs


@pytest.mark.unit
async def test_backfill_writes_sentinel_after_successful_scroll(mocker):
    """Successful backfill writes a sentinel point so future restarts skip."""
    client = mocker.AsyncMock()
    client.retrieve.return_value = []  # No sentinel — backfill must run
    client.scroll.return_value = ([_record(1, "abc")], None)

    await _backfill_doc_id_to_string(client, "test-collection", _backfill_dimension())

    # Single upsert with the sentinel UUID + migration marker payload.
    assert client.upsert.await_count == 1
    upsert_kwargs = client.upsert.await_args.kwargs
    assert upsert_kwargs["collection_name"] == "test-collection"
    assert upsert_kwargs["wait"] is True
    points = upsert_kwargs["points"]
    assert len(points) == 1
    assert points[0].id == _DOC_ID_BACKFILL_SENTINEL_ID
    assert points[0].payload == {"_migration_marker": "doc_id_v1"}


@pytest.mark.unit
async def test_backfill_rewrites_int_doc_ids_to_str(mocker):
    """Mixed int/str payload across two scroll pages: only ints get rewritten."""
    client = mocker.AsyncMock()
    client.retrieve.return_value = []
    # Two scroll calls: batch 1 is mixed and reports a next_offset; batch 2
    # is mixed with next_offset=None to terminate.
    client.scroll.side_effect = [
        ([_record(1, 100), _record(2, "abc")], "next-offset-123"),
        ([_record(3, 200), _record(4, "def")], None),
    ]

    await _backfill_doc_id_to_string(client, "test-collection", _backfill_dimension())

    # One set_payload per *unique* int value — point 1 (100) and point 3
    # (200) are in different batches with different values, so two calls.
    assert client.set_payload.await_count == 2
    client.set_payload.assert_any_await(
        collection_name="test-collection",
        payload={"doc_id": "100"},
        points=[1],
        wait=True,
    )
    client.set_payload.assert_any_await(
        collection_name="test-collection",
        payload={"doc_id": "200"},
        points=[3],
        wait=True,
    )


@pytest.mark.unit
async def test_backfill_batches_points_with_same_doc_id(mocker):
    """Multiple points sharing the same int doc_id collapse to one set_payload.

    A single document indexed as multiple chunks all share its doc_id; the
    backfill should issue one set_payload call covering the chunk batch.
    """
    client = mocker.AsyncMock()
    client.retrieve.return_value = []
    client.scroll.side_effect = [
        (
            [
                _record(10, 42),
                _record(11, 42),
                _record(12, 42),
                _record(13, "already-str"),
            ],
            None,
        ),
    ]

    await _backfill_doc_id_to_string(client, "test-collection", _backfill_dimension())

    # All three int-payload points share doc_id=42, so a single call covers them.
    assert client.set_payload.await_count == 1
    client.set_payload.assert_awaited_with(
        collection_name="test-collection",
        payload={"doc_id": "42"},
        points=[10, 11, 12],
        wait=True,
    )


@pytest.mark.unit
async def test_backfill_emits_completion_log(mocker, caplog):
    """Backfill logs final rewritten/scanned counts at INFO."""
    client = mocker.AsyncMock()
    client.retrieve.return_value = []
    client.scroll.side_effect = [
        ([_record(1, 7), _record(2, "x")], None),
    ]

    with caplog.at_level("INFO", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _backfill_doc_id_to_string(
            client, "test-collection", _backfill_dimension()
        )

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
    client.retrieve.return_value = []
    client.scroll.side_effect = [
        ([_record(1, None), _record(2, 99)], None),
    ]

    await _backfill_doc_id_to_string(client, "test-collection", _backfill_dimension())

    # Only the int doc_id at point 2 was rewritten; the None-payload point was skipped.
    assert client.set_payload.await_count == 1
    client.set_payload.assert_awaited_with(
        collection_name="test-collection",
        payload={"doc_id": "99"},
        points=[2],
        wait=True,
    )


@pytest.mark.unit
async def test_backfill_handles_payload_with_explicit_none_doc_id(mocker):
    """A payload of {doc_id: None, ...} is skipped just like payload=None."""
    client = mocker.AsyncMock()
    client.retrieve.return_value = []
    # Build the record manually to distinguish payload=None from payload={"doc_id": None}.
    point_with_explicit_none = SimpleNamespace(
        id=1, payload={"doc_id": None, "doc_type": "file"}
    )
    client.scroll.side_effect = [
        ([point_with_explicit_none, _record(2, 99)], None),
    ]

    await _backfill_doc_id_to_string(client, "test-collection", _backfill_dimension())

    # Only the int doc_id at point 2 was rewritten; the explicit-None payload was skipped.
    assert client.set_payload.await_count == 1
    client.set_payload.assert_awaited_with(
        collection_name="test-collection",
        payload={"doc_id": "99"},
        points=[2],
        wait=True,
    )
