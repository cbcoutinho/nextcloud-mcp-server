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

import anyio
import httpx
import pytest
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import PayloadSchemaType

from nextcloud_mcp_server.vector import qdrant_client as qdrant_module
from nextcloud_mcp_server.vector.qdrant_client import (
    _DOC_ID_BACKFILL_SENTINEL_ID,
    _PAYLOAD_INDEX_FIELDS,
    _backfill_doc_id_to_string,
    _ensure_payload_indexes,
    _group_int_doc_ids,
    get_qdrant_client,
)


def _empty_collection_info() -> SimpleNamespace:
    """Stand-in for a CollectionInfo with no payload indexes yet.

    Tests for _ensure_payload_indexes only read ``payload_schema``
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
# _PAYLOAD_INDEX_FIELDS contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_modified_at_indexed_as_integer():
    """ADR-027: the date-range filter needs a numeric index on modified_at.

    INTEGER (not FLOAT/DATETIME) because the payload stores an int Unix-second
    timestamp; a numeric Range filters it without a content re-index.
    """
    assert _PAYLOAD_INDEX_FIELDS.get("modified_at") == PayloadSchemaType.INTEGER


@pytest.mark.unit
def test_file_path_indexed_as_text():
    """ADR-027 Phase 2: the path filter uses MatchText, which needs a TEXT index
    on file_path (server Qdrant); local qdrant-client matches by substring."""
    assert _PAYLOAD_INDEX_FIELDS.get("file_path") == PayloadSchemaType.TEXT


# ---------------------------------------------------------------------------
# _ensure_payload_indexes
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_ensure_payload_indexes_creates_each_field(mocker):
    """Happy path: every field in _PAYLOAD_INDEX_FIELDS gets its declared schema.

    The dict-of-(field, schema_type) registry pairs string fields with
    KEYWORD and the boolean ``is_placeholder`` with BOOL — both are
    required because Qdrant's strict-mode index-required filtering
    enforces a payload index for any ``FieldCondition`` regardless of
    value type.
    """
    client = mocker.AsyncMock()
    client.get_collection.return_value = _empty_collection_info()

    await _ensure_payload_indexes(client, "test-collection")

    assert client.create_payload_index.await_count == len(_PAYLOAD_INDEX_FIELDS)
    expected_calls = [
        call(
            collection_name="test-collection",
            field_name=field,
            field_schema=schema_type,
            wait=True,
        )
        for field, schema_type in _PAYLOAD_INDEX_FIELDS.items()
    ]
    client.create_payload_index.assert_has_awaits(expected_calls, any_order=False)


@pytest.mark.unit
async def test_ensure_payload_indexes_includes_is_placeholder_as_bool(mocker):
    """is_placeholder must be created with BOOL schema, not KEYWORD.

    ``get_placeholder_filter`` and ``delete_placeholder_point`` filter on
    ``is_placeholder`` (a bool); creating it with KEYWORD would still
    fail strict-mode index-required filtering on Qdrant Cloud because
    the index type wouldn't match the value type.
    """
    client = mocker.AsyncMock()
    client.get_collection.return_value = _empty_collection_info()

    await _ensure_payload_indexes(client, "test-collection")

    bool_calls = [
        c
        for c in client.create_payload_index.await_args_list
        if c.kwargs.get("field_name") == "is_placeholder"
    ]
    assert len(bool_calls) == 1, "is_placeholder must be created exactly once"
    assert bool_calls[0].kwargs["field_schema"] is PayloadSchemaType.BOOL


@pytest.mark.unit
async def test_ensure_payload_indexes_skips_fields_already_indexed(mocker, caplog):
    """Routine restart path: existing payload indexes are silently skipped.

    Without the pre-fetch, every restart logs `Created <SCHEMA> payload
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
        await _ensure_payload_indexes(client, "test-collection")

    # Only the missing fields are created — every entry in the registry
    # other than the one already in the schema.
    expected_missing = set(_PAYLOAD_INDEX_FIELDS) - {"doc_id"}
    assert client.create_payload_index.await_count == len(expected_missing)
    created_fields = {
        c.kwargs["field_name"] for c in client.create_payload_index.await_args_list
    }
    assert created_fields == expected_missing
    # No INFO log fires for the already-indexed field.
    info_messages = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert not any("doc_id" in m for m in info_messages), info_messages


@pytest.mark.unit
async def test_ensure_payload_indexes_warns_on_wrong_schema_type(mocker, caplog):
    """Pre-existing index with wrong schema type surfaces as a WARNING.

    The bug this PR fixes: a collection migrated from the int-doc_id era
    can have ``doc_id`` indexed as INTEGER, which silently survives the
    "field already in schema → skip" branch and lets ``MatchValue(value="123")``
    keep failing with HTTP 400 on Qdrant Cloud strict mode. Confirm the
    type-aware check fires a WARNING, marks the field as failed (so the
    consolidated end-of-function summary picks it up), and does NOT
    attempt to recreate the index — operator intervention is the only
    safe path.
    """
    client = mocker.AsyncMock()
    # PayloadIndexInfo-like stand-in: only ``data_type`` is read.
    wrong = SimpleNamespace(data_type=PayloadSchemaType.INTEGER)
    client.get_collection.return_value = SimpleNamespace(
        payload_schema={"doc_id": wrong}
    )

    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_payload_indexes(client, "test-collection")

    # No create attempt for the mismatched field.
    created_fields = {
        c.kwargs["field_name"] for c in client.create_payload_index.await_args_list
    }
    assert "doc_id" not in created_fields

    warning_messages = [
        r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    ]
    # Per-field warning describes both observed and expected types.
    assert any(
        "doc_id" in m and "INTEGER" in m and "KEYWORD" in m for m in warning_messages
    ), warning_messages
    # Consolidated summary at end of function includes the field too.
    assert any(
        "Payload index creation incomplete" in m and "doc_id" in m
        for m in warning_messages
    ), warning_messages


@pytest.mark.unit
async def test_ensure_payload_indexes_logs_400_as_warning(mocker, caplog):
    """A 400 from create_payload_index logs WARNING *and* fires the summary.

    Real Qdrant returns 200 when the index already exists with a matching
    schema, so 400s indicate a genuine problem (e.g., schema conflict on a
    pre-existing index). The loop continues past the failure so the
    remaining fields still get indexed, *and* the field accumulates into
    ``failed_fields`` so the consolidated `Payload index creation
    incomplete` summary fires — without this, tenants whose
    ``payload_schema`` is hidden from their JWT (Qdrant Cloud
    collection-scoped tokens) would only see the per-field warning and
    miss the operator-level summary that `wrong_schema_type` paths emit.
    """
    client = mocker.AsyncMock()
    client.get_collection.return_value = _empty_collection_info()
    # First field fails with 400; remaining fields succeed. One side_effect
    # entry per item in _PAYLOAD_INDEX_FIELDS so the iteration is exhaustive.
    client.create_payload_index.side_effect = [
        _make_unexpected(
            400,
            b'{"status":{"error":"field \\"doc_id\\" indexed with different schema"}}',
        ),
        *([None] * (len(_PAYLOAD_INDEX_FIELDS) - 1)),
    ]

    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_payload_indexes(client, "test-collection")

    # Loop continued past the failing field; every field was attempted.
    assert client.create_payload_index.await_count == len(_PAYLOAD_INDEX_FIELDS)
    warning_messages = [
        r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    ]
    # Per-field warning describes the schema conflict.
    assert any(
        m.startswith("Schema conflict on payload index") and "different schema" in m
        for m in warning_messages
    ), warning_messages
    # Consolidated summary names the failed field too — see the docstring
    # for why this matters in tenant-scoped Qdrant Cloud setups.
    first_field = next(iter(_PAYLOAD_INDEX_FIELDS))
    assert any(
        "Payload index creation incomplete" in m and first_field in m
        for m in warning_messages
    ), warning_messages


@pytest.mark.unit
async def test_ensure_payload_indexes_logs_non_400_as_error(mocker, caplog):
    """A non-400 status from create_payload_index escalates to ERROR.

    A 5xx response (e.g., Qdrant temporarily unavailable) should not be
    silently downgraded to a warning the way a 400 schema-conflict is.
    The loop still continues so the remaining fields get attempted.
    """
    client = mocker.AsyncMock()
    client.get_collection.return_value = _empty_collection_info()
    # First field fails with 500; remaining fields succeed.
    client.create_payload_index.side_effect = [
        _make_unexpected(500, b'{"status":{"error":"internal server error"}}'),
        *([None] * (len(_PAYLOAD_INDEX_FIELDS) - 1)),
    ]

    with caplog.at_level("ERROR", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_payload_indexes(client, "test-collection")

    assert client.create_payload_index.await_count == len(_PAYLOAD_INDEX_FIELDS)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert "500" in msg
    assert "internal server error" in msg


@pytest.mark.unit
async def test_ensure_payload_indexes_continues_past_raw_network_error(mocker, caplog):
    """A raw network error (e.g. ConnectError, TimeoutError) must not skip the rest.

    UnexpectedResponse covers HTTP-shaped failures, but transport-level
    failures (httpx.ConnectError, asyncio.TimeoutError) reach the loop as
    bare Exceptions. Without a broad catch, the first network blip
    propagates, leaves _qdrant_client assigned, and silently skips every
    remaining field. The fix is per-field containment matching the 5xx
    behaviour: log at ERROR with exc_info, append to failed_fields, and
    continue.
    """
    client = mocker.AsyncMock()
    client.get_collection.return_value = _empty_collection_info()
    # First field hits a connection failure; remaining fields succeed.
    client.create_payload_index.side_effect = [
        ConnectionError("Connection refused"),
        *([None] * (len(_PAYLOAD_INDEX_FIELDS) - 1)),
    ]

    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_payload_indexes(client, "test-collection")

    # Loop continued past the failing field; every field was attempted.
    assert client.create_payload_index.await_count == len(_PAYLOAD_INDEX_FIELDS)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "Network error creating payload index" in errors[0].getMessage()
    # exc_info is preserved so operators can see the underlying cause.
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[0] is ConnectionError
    # The partial-failure summary surfaces the field as missing.
    summary_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and "Payload index creation incomplete" in r.getMessage()
    ]
    assert len(summary_warnings) == 1
    # The first field in _PAYLOAD_INDEX_FIELDS is the one that raised.
    failing_field = next(iter(_PAYLOAD_INDEX_FIELDS))
    assert failing_field in summary_warnings[0].getMessage()


@pytest.mark.unit
async def test_ensure_payload_indexes_logs_and_returns_when_get_collection_raises(
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
        await anyio.lowlevel.checkpoint()
        raise RuntimeError("connection refused")

    client.get_collection.side_effect = _get_collection_raises

    with caplog.at_level("ERROR", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_payload_indexes(client, "test-collection")

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
    """Mixed int/str payload across two scroll pages: only ints get rewritten.

    Includes ``doc_id=0`` to guard against a future "early-exit on
    falsy" refactor — the helper must rewrite zero alongside other
    ints, not skip it.
    """
    client = mocker.AsyncMock()
    client.retrieve.return_value = []
    # Two scroll calls: batch 1 mixes int (incl. 0) + str and reports a
    # next_offset; batch 2 mixes int + str with next_offset=None to terminate.
    client.scroll.side_effect = [
        (
            [_record(1, 100), _record(2, "abc"), _record(5, 0)],
            "next-offset-123",
        ),
        ([_record(3, 200), _record(4, "def")], None),
    ]

    await _backfill_doc_id_to_string(client, "test-collection", _backfill_dimension())

    # One set_payload per *unique* int value across all batches: 100, 0, 200.
    assert client.set_payload.await_count == 3
    client.set_payload.assert_any_await(
        collection_name="test-collection",
        payload={"doc_id": "100"},
        points=[1],
        wait=True,
    )
    client.set_payload.assert_any_await(
        collection_name="test-collection",
        payload={"doc_id": "0"},
        points=[5],
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
    # The `await anyio.lowlevel.checkpoint()` is a no-op event-loop yield that
    # satisfies static analysis ("async function uses no async features")
    # without changing observable behavior.
    async def _scroll_raises(*args, **kwargs):
        await anyio.lowlevel.checkpoint()
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
        await anyio.lowlevel.checkpoint()
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
    # Real Qdrant returns next_offset as a UUID string (or None to terminate).
    # Match that shape so the stub remains accurate if scroll's return type is
    # ever tightened — and aligns with test_backfill_rewrites_int_doc_ids_to_str.
    batches: list[tuple[list[SimpleNamespace], str | None]] = [
        ([str_point], "next-1") for _ in range(21)
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


# ---------------------------------------------------------------------------
# _group_int_doc_ids
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_group_int_doc_ids_skips_float_and_warns(caplog):
    """A float doc_id is not stringified; it logs WARNING and is skipped.

    Producers always write int or str. A float would round-trip to e.g.
    ``"3.0"``, which the keyword index and verification path
    (``int(doc_id)``) would never match. Skipping with a loud warning is
    the only safe choice.
    """
    float_point = SimpleNamespace(id=99, payload={"doc_id": 3.0})
    int_point = SimpleNamespace(id=42, payload={"doc_id": 7})

    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.vector.qdrant_client"):
        by_value, scanned = _group_int_doc_ids([float_point, int_point])

    # Only the int point made it into by_value; float was dropped.
    assert by_value == {"7": [42]}
    # Both points still count toward the scanned total — the warning
    # should not hide them from progress logs.
    assert scanned == 2

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "float" in msg
    assert "99" in msg


@pytest.mark.unit
def test_group_int_doc_ids_skips_bool_and_warns(caplog):
    """A bool doc_id is not stringified to "True"/"False"; it logs and skips.

    ``isinstance(True, int)`` is ``True`` because ``bool`` is a subclass of
    ``int`` in Python, so a naive ``isinstance(value, int)`` guard would let
    a boolean payload through and write ``str(True)`` → ``"True"`` into
    Qdrant. Producers never write bools, but the strict ``type(value) is
    int`` guard ensures any future producer bug surfaces as a WARNING and is
    not silently stringified.
    """
    bool_point = SimpleNamespace(id=33, payload={"doc_id": True})
    int_point = SimpleNamespace(id=42, payload={"doc_id": 7})

    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.vector.qdrant_client"):
        by_value, scanned = _group_int_doc_ids([bool_point, int_point])

    # Only the int point made it into by_value — "True" is *not* a key.
    assert by_value == {"7": [42]}
    assert "True" not in by_value
    assert "False" not in by_value
    assert scanned == 2

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "bool" in msg
    assert "33" in msg


@pytest.mark.unit
def test_group_int_doc_ids_handles_str_and_missing_silently(caplog):
    """str / missing doc_id payloads are skipped without warning.

    These are the steady-state paths — already-migrated str values and
    sentinel-style points without a doc_id key. Neither should noise up
    the log on every restart.
    """
    str_point = SimpleNamespace(id=1, payload={"doc_id": "abc"})
    none_payload_point = SimpleNamespace(id=2, payload=None)
    missing_key_point = SimpleNamespace(id=3, payload={"other": "value"})
    explicit_none_point = SimpleNamespace(id=4, payload={"doc_id": None})

    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.vector.qdrant_client"):
        by_value, scanned = _group_int_doc_ids(
            [str_point, none_payload_point, missing_key_point, explicit_none_point]
        )

    assert by_value == {}
    assert scanned == 4
    # No warnings — these paths are expected and silent.
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.mark.unit
def test_group_int_doc_ids_groups_ints_by_str_value():
    """Multiple int-doc_id points sharing a value collapse into one entry.

    Pins the chunk-batching contract: all chunks of one document share its
    doc_id, so the helper hands ``_apply_backfill_writes`` a single key
    with all chunk point-ids attached.
    """
    by_value, scanned = _group_int_doc_ids(
        [
            SimpleNamespace(id=10, payload={"doc_id": 42}),
            SimpleNamespace(id=11, payload={"doc_id": 42}),
            SimpleNamespace(id=12, payload={"doc_id": 7}),
        ]
    )

    assert by_value == {"42": [10, 11], "7": [12]}
    assert scanned == 3


@pytest.mark.unit
async def test_ensure_payload_indexes_summarises_failed_fields(mocker, caplog):
    """A non-400 failure surfaces both as ERROR and a WARNING summary.

    Per-field ERROR lines are easy to miss in startup noise; the
    WARNING summary at the end of the loop names every field that
    didn't get an index, so operators auditing the log can spot the
    degraded state at a glance.
    """
    client = mocker.AsyncMock()
    client.get_collection.return_value = SimpleNamespace(payload_schema={})
    # _PAYLOAD_INDEX_FIELDS preserves insertion order; user_id is the second
    # entry, so call #2 is the success case and every other field fails. Don't
    # hard-code the full field list here — it grows as new fields move into
    # the index dict, and the assertions below are what enforce coverage.
    call_count = {"n": 0}

    async def _create_index(*args, **kwargs):
        # See _scroll_raises above for why this is async + sleep(0).
        await anyio.lowlevel.checkpoint()
        call_count["n"] += 1
        if call_count["n"] != 2:
            raise _make_unexpected(500, b'{"status":{"error":"boom"}}')
        return None

    client.create_payload_index.side_effect = _create_index

    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.vector.qdrant_client"):
        await _ensure_payload_indexes(client, "test-collection")

    summary = [
        r.getMessage()
        for r in caplog.records
        if "Payload index creation incomplete" in r.getMessage()
    ]
    assert len(summary) == 1
    # Every field that failed must appear in the summary — operators rely on
    # this single log line to spot the degraded state, so any missing entry
    # is a silent gap.
    assert "doc_id" in summary[0]
    assert "doc_type" in summary[0]
    assert "is_placeholder" in summary[0]
    assert "chunk_index" in summary[0]
    assert "chunk_start_offset" in summary[0]
    assert "chunk_end_offset" in summary[0]
    assert "user_id" not in summary[0]  # The one that succeeded.
    assert "test-collection" in summary[0]


# ---------------------------------------------------------------------------
# get_qdrant_client — collection-existence probe across modes
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_qdrant_singleton():
    """Reset the module-level singleton + init lock around each test.

    ``get_qdrant_client`` short-circuits on a non-None ``_qdrant_client``
    via the unsynchronized fast path, so any prior test that initialised
    the singleton would mask the cold-start logic these tests exercise.
    Restore the original after the test so a leak doesn't bleed into
    later tests in the same process.
    """
    original_client = qdrant_module._qdrant_client
    original_lock = qdrant_module._qdrant_init_lock
    qdrant_module._qdrant_client = None
    qdrant_module._qdrant_init_lock = None
    yield
    qdrant_module._qdrant_client = original_client
    qdrant_module._qdrant_init_lock = original_lock


def _stub_provisional(mocker, get_collection_side_effect):
    """Build a fake AsyncQdrantClient suitable for cold-start get_qdrant_client.

    ``get_collection`` is wired up to ``get_collection_side_effect``;
    every other awaited method returns an AsyncMock so the migration
    helpers (``_ensure_payload_indexes`` etc.) don't blow up on the
    create-collection path. Returns the mock so tests can assert against
    the awaited methods.
    """
    provisional = mocker.AsyncMock()
    provisional.get_collection.side_effect = get_collection_side_effect
    # _ensure_payload_indexes pulls payload_schema off the freshly-created
    # collection's get_collection result; on the create path it's passed
    # an explicit {} so this branch isn't exercised, but make it safe
    # anyway in case the order of init shifts.
    provisional.create_payload_index.return_value = None
    return provisional


def _stub_settings_and_embedding(mocker, monkeypatch):
    """Replace get_settings and the embedding service with deterministic stubs."""
    from nextcloud_mcp_server.config import Settings

    settings = Settings(
        qdrant_location=":memory:",
        ollama_embedding_model="nomic-embed-text",
        vector_sync_enabled=False,
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.qdrant_client.get_settings", lambda: settings
    )

    embedding_service = mocker.Mock()
    # No _detect_dimension attribute → the dynamic-detection branch is
    # skipped. Real Ollama provider has it, but tests don't need to.
    embedding_service.provider = mocker.Mock(spec_set=[])
    embedding_service.get_dimension = lambda: 4
    monkeypatch.setattr(
        "nextcloud_mcp_server.embedding.get_embedding_service",
        lambda: embedding_service,
    )
    return settings


@pytest.mark.unit
async def test_get_qdrant_client_creates_collection_on_local_mode_value_error(
    mocker, monkeypatch, reset_qdrant_singleton
):
    """Local-mode `ValueError("Collection X not found")` must trigger create.

    The local/in-memory ``AsyncQdrantClient`` raises ``ValueError`` (see
    ``qdrant_client/local/async_qdrant_local.py``) where the HTTP-mode
    client would raise ``UnexpectedResponse(status_code=404)``. Both must
    be treated as "the collection doesn't exist yet — create it."
    Without this dual-path catch, the ``mcp`` container fails on first
    start with `Failed to initialize Qdrant collection: Collection X not
    found` and the ``app.py`` lifespan re-raises as ``RuntimeError``,
    crashing every single-user / login-flow / multi-user-basic CI job.
    """
    settings = _stub_settings_and_embedding(mocker, monkeypatch)
    collection_name = settings.get_collection_name()

    provisional = _stub_provisional(
        mocker, ValueError(f"Collection {collection_name} not found")
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.qdrant_client.AsyncQdrantClient",
        lambda *a, **kw: provisional,
    )

    client = await get_qdrant_client()

    assert client is provisional
    provisional.create_collection.assert_awaited_once()
    # The created collection should be the auto-generated name from
    # settings — guards against accidental collection-name drift.
    assert (
        provisional.create_collection.await_args.kwargs["collection_name"]
        == collection_name
    )


@pytest.mark.unit
async def test_get_qdrant_client_propagates_unrelated_value_error(
    mocker, monkeypatch, reset_qdrant_singleton
):
    """A ValueError that is *not* a missing-collection signal must propagate.

    The ``except ValueError`` clause in ``get_qdrant_client`` matches on
    the ``"not found"`` substring rather than catching every
    ``ValueError`` so genuine programming bugs (bad ``collection_name``
    validation, dimension assertions, etc.) still surface to the caller.
    Loosening the guard to a bare ``except ValueError`` would silently
    treat any of those as "create the collection" and mask the bug.
    """
    _stub_settings_and_embedding(mocker, monkeypatch)
    provisional = _stub_provisional(mocker, ValueError("Bad collection_name"))
    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.qdrant_client.AsyncQdrantClient",
        lambda *a, **kw: provisional,
    )

    with pytest.raises(ValueError, match="Bad collection_name"):
        await get_qdrant_client()

    provisional.create_collection.assert_not_awaited()
