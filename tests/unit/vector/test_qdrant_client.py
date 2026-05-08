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

import asyncio
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
    # 400s do not contribute to the partial-failure summary (which fires
    # only for non-400 errors), so this is the per-field warning, not the
    # summary. Match the message prefix exactly so a future change adding
    # 400s to the summary would surface here as a count mismatch.
    assert len(warnings) == 1
    assert warnings[0].getMessage().startswith("Schema conflict on payload index")
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


@pytest.mark.unit
async def test_ensure_keyword_payload_indexes_logs_and_returns_when_get_collection_raises(
    mocker, caplog
):
    """A get_collection failure is logged and swallowed; no indexes are attempted.

    Mirrors the broad swallow in `_backfill_doc_id_to_string`. The
    qdrant_client singleton is already assigned by the time this
    function runs, so re-raising would leave the process holding a
    usable client with the migration silently skipped on every
    subsequent call. Catching, logging, and returning preserves the
    retry-on-next-restart behavior.
    """
    client = mocker.AsyncMock()

    async def _get_collection_raises(*args, **kwargs):
        # See _scroll_raises in the backfill section for why this is async.
        await asyncio.sleep(0)
        raise RuntimeError("connection refused")

    client.get_collection.side_effect = _get_collection_raises

    with caplog.at_level("ERROR", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_keyword_payload_indexes(client, "test-collection")

    # No index creation was attempted — the function returned early.
    client.create_payload_index.assert_not_awaited()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert "Failed to fetch collection info for 'test-collection'" in msg
    assert "Will retry on next restart" in msg
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[0] is RuntimeError


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
    # rewritten=0 → human-readable wording instead of the misleading
    # "rewrote 0/N from int to str" formula.
    assert "2 points scanned" in completion_logs[0]
    assert "none required rewriting" in completion_logs[0]


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


@pytest.mark.unit
async def test_backfill_logs_and_returns_when_scroll_raises(mocker, caplog):
    """A scroll-time exception is logged and swallowed; sentinel is not written.

    The singleton client in get_qdrant_client is already assigned by the
    time _backfill_doc_id_to_string runs, so re-raising here would leave
    the process holding a usable client with the migration silently
    skipped on every subsequent call. Catching, logging, and returning
    without writing the sentinel preserves retry-on-next-restart behavior.
    """
    client = mocker.AsyncMock()
    client.retrieve.return_value = []  # No sentinel — backfill must run

    # An async-callable side_effect lets AsyncMock await the coroutine
    # before the exception propagates; assigning a bare exception class
    # leaks an un-awaited coroutine and trips RuntimeWarning at gc time.
    # The `await asyncio.sleep(0)` is a no-op event-loop yield that
    # satisfies static analysis ("async function uses no async features")
    # without changing observable behavior.
    async def _scroll_raises(*args, **kwargs):
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    client.scroll.side_effect = _scroll_raises

    with caplog.at_level("ERROR", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _backfill_doc_id_to_string(
            client, "test-collection", _backfill_dimension()
        )

    # No sentinel written — next process restart will retry from scratch.
    client.upsert.assert_not_awaited()
    client.set_payload.assert_not_awaited()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "doc_id backfill scroll failed" in errors[0].getMessage()
    assert "test-collection" in errors[0].getMessage()
    # exc_info=True attaches the original exception to the log record.
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[0] is RuntimeError


@pytest.mark.unit
async def test_backfill_logs_warning_when_sentinel_upsert_fails(mocker, caplog):
    """Sentinel-write failure after a successful scroll logs WARNING, not ERROR.

    A failure here means the data migration succeeded but the
    short-circuit marker is missing. The data is correct; only the
    marker is absent, so the next restart will re-scroll an
    already-clean collection (idempotent zero-write) and retry the
    upsert. Differentiating this from a genuine scroll failure prevents
    an "ERROR — backfill failed" log line that contradicts the
    successful data state.
    """
    client = mocker.AsyncMock()
    client.retrieve.return_value = []  # No sentinel — backfill must run
    client.scroll.return_value = ([], None)  # Empty scroll — clean collection

    async def _upsert_raises(*args, **kwargs):
        # See _scroll_raises above for why this is async + sleep(0).
        await asyncio.sleep(0)
        raise RuntimeError("sentinel write blip")

    client.upsert.side_effect = _upsert_raises

    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _backfill_doc_id_to_string(
            client, "test-collection", _backfill_dimension()
        )

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "sentinel write failed" in warnings[0].getMessage()
    assert "test-collection" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None
    assert warnings[0].exc_info[0] is RuntimeError
    # No ERROR — data state is correct, not a backfill failure.
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


@pytest.mark.unit
async def test_backfill_emits_progress_log_every_20_batches(mocker, caplog):
    """Long scrolls emit a progress INFO line every 20 batches.

    Operators auditing a 50k+ point collection's startup migration need
    proof the server isn't hung; a single start/end pair leaves a
    minutes-long silence in the log. The progress line carries the
    collection name, scanned count, and rewritten count so the same
    log message also acts as a heartbeat.
    """
    client = mocker.AsyncMock()
    client.retrieve.return_value = []

    # Return 21 non-empty batches followed by an empty one to terminate
    # the loop; every batch contains points already in str form so no
    # set_payload calls happen — the test focuses on the progress log
    # cadence, not the rewrite path.
    str_point = SimpleNamespace(id=1, payload={"doc_id": "abc"})
    batches: list[tuple[list[SimpleNamespace], int | None]] = [
        ([str_point], 1) for _ in range(21)
    ] + [([], None)]
    client.scroll.side_effect = batches

    with caplog.at_level("INFO", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _backfill_doc_id_to_string(
            client, "test-collection", _backfill_dimension()
        )

    progress_messages = [
        r.getMessage()
        for r in caplog.records
        if "doc_id backfill progress on" in r.getMessage()
    ]
    # 21 batches → exactly one progress line at batch 20.
    assert len(progress_messages) == 1
    assert "scanned 20 points" in progress_messages[0]
    assert "test-collection" in progress_messages[0]


@pytest.mark.unit
async def test_ensure_keyword_payload_indexes_summarises_failed_fields(mocker, caplog):
    """A non-400 failure surfaces both as ERROR and a WARNING summary.

    Per-field ERROR lines are easy to miss in startup noise; the
    WARNING summary at the end of the loop names every field that
    didn't get an index, so operators auditing the log can spot the
    degraded state at a glance.
    """
    client = mocker.AsyncMock()
    client.get_collection.return_value = SimpleNamespace(payload_schema={})
    # Two of the three fields fail with 5xx; one succeeds.
    call_count = {"n": 0}

    async def _create_index(*args, **kwargs):
        # See _scroll_raises above for why this is async + sleep(0).
        await asyncio.sleep(0)
        call_count["n"] += 1
        if call_count["n"] != 2:
            raise _make_unexpected(500, b'{"status":{"error":"boom"}}')
        return None

    client.create_payload_index.side_effect = _create_index

    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_keyword_payload_indexes(client, "test-collection")

    summary = [
        r.getMessage()
        for r in caplog.records
        if "Payload index creation incomplete" in r.getMessage()
    ]
    assert len(summary) == 1
    # Field order matches _KEYWORD_PAYLOAD_FIELDS = ("doc_id", "user_id", "doc_type")
    assert "doc_id" in summary[0]
    assert "doc_type" in summary[0]
    assert "user_id" not in summary[0]  # The one that succeeded.
    assert "test-collection" in summary[0]
