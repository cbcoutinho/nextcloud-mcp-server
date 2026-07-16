"""Unit tests for ``UsageEventStore`` (Deck #67 usage metering, data plane).

Parametrized over both supported backends via the shared ``storage_backend``
fixture: SQLite (default, always runs) and Postgres (opt-in, gated on
``TEST_DATABASE_URL`` — bring up ``docker compose --profile postgres up -d
postgres-test`` and export
``TEST_DATABASE_URL=postgresql+psycopg://mcp:mcp@localhost:5433/mcp``).

Covers the recording contract: flag-gated no-op, insert roundtrip, ON CONFLICT
dedup, JSON metadata roundtrip, NULL metadata, and the best-effort guarantee
that a DB failure is swallowed instead of surfacing to the caller.
"""

import json
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import nextcloud_mcp_server.usage.store as store_module
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.observability.metrics import db_connect_duration_seconds
from nextcloud_mcp_server.usage.store import UsageEvent, UsageEventStore

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_shared_usage_store():
    """Keep the process-wide ``shared()`` cache from leaking across tests.

    These tests construct ``UsageEventStore(storage)`` directly, but a stray
    ``shared()`` call (here or in a smoke test sharing the process) would
    otherwise poison later tests with a stale storage handle.
    """
    UsageEventStore._shared_instance = None
    yield
    UsageEventStore._shared_instance = None


@pytest.fixture
async def storage(storage_backend):
    """Initialized RefreshTokenStorage backed by SQLite or Postgres."""
    key = Fernet.generate_key()
    if storage_backend["kind"] == "sqlite":
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "usage.db"
            s = RefreshTokenStorage(db_path=str(db_path), encryption_key=key)
            await s.initialize()
            yield s
    else:
        s = RefreshTokenStorage(database_url=storage_backend["url"], encryption_key=key)
        await s.initialize()
        try:
            yield s
        finally:
            await storage_backend["reset"]()


def _set_metering(monkeypatch, enabled: bool) -> None:
    """Force the metering flag without mutating global dynaconf state.

    ``record_usage_event`` calls ``get_settings()`` (imported into the store
    module's namespace), and ``get_settings()`` builds a fresh Settings per
    call — so patching the symbol the store sees is the clean seam.
    """

    class _Settings:
        usage_metering_enabled = enabled

    monkeypatch.setattr(store_module, "get_settings", lambda: _Settings())


def _connect_observations(dialect: str) -> float:
    """How many connects the ``mcp_db_connect_duration_seconds`` histogram saw.

    Read as a delta around the call under test: the registry is process-wide and
    other tests in the session also open connections.
    """
    for metric in db_connect_duration_seconds.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count") and sample.labels.get("db") == dialect:
                return sample.value
    return 0.0


async def _count(storage: RefreshTokenStorage) -> int:
    async with storage.acquire() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM usage_events")
        row = await cursor.fetchone()
    return row[0]


async def _fetch(storage: RefreshTokenStorage, event_id: str):
    async with storage.acquire() as db:
        cursor = await db.execute(
            "SELECT event_id, occurred_at, metric, value, metadata "
            "FROM usage_events WHERE event_id = ?",
            (event_id,),
        )
        return await cursor.fetchone()


async def test_flag_off_is_noop(storage, monkeypatch):
    """With metering disabled, nothing is written (zero DB work)."""
    _set_metering(monkeypatch, False)
    store = UsageEventStore(storage)
    await store.record_usage_event(metric="pages_embedded", value=5)
    assert await _count(storage) == 0


async def test_enabled_param_short_circuits_without_reading_settings(
    storage, monkeypatch
):
    """An explicit ``enabled`` flag is honored without touching get_settings().

    Hot-path callers pass the already-resolved flag; the store must not rebuild
    Settings when given one. ``enabled=False`` is a no-op; ``enabled=True``
    writes even though the (boobytrapped) settings lookup would raise.
    """

    def _boom():
        raise AssertionError("get_settings() must not be called when enabled is passed")

    monkeypatch.setattr(store_module, "get_settings", _boom)
    store = UsageEventStore(storage)

    await store.record_usage_event(metric="pages_embedded", value=1, enabled=False)
    assert await _count(storage) == 0

    eid = str(uuid.uuid4())
    await store.record_usage_event(
        metric="pages_embedded", value=1, event_id=eid, enabled=True
    )
    assert await _count(storage) == 1


async def test_insert_roundtrip(storage, monkeypatch):
    """A recorded event lands and reads back with the right fields."""
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)
    eid = str(uuid.uuid4())
    await store.record_usage_event(
        metric="pages_embedded",
        value=7,
        event_id=eid,
        metadata={"provider": "gateway"},
    )
    row = await _fetch(storage, eid)
    assert row is not None
    # Postgres returns event_id as a uuid.UUID; normalize to str for compare.
    assert str(row[0]) == eid
    assert row[2] == "pages_embedded"
    assert row[3] == 7


async def test_on_conflict_dedup(storage, monkeypatch):
    """A duplicate event_id is a no-op; the first write is retained."""
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)
    eid = str(uuid.uuid4())
    await store.record_usage_event(metric="pages_embedded", value=1, event_id=eid)
    await store.record_usage_event(metric="tokens_embedded", value=99, event_id=eid)
    assert await _count(storage) == 1
    row = await _fetch(storage, eid)
    assert row[2] == "pages_embedded"  # DO NOTHING, not DO UPDATE
    assert row[3] == 1


async def test_metadata_json_roundtrip(storage, monkeypatch):
    """Nested metadata round-trips as JSON on both backends."""
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)
    eid = str(uuid.uuid4())
    meta = {"provider": "gateway", "model": "titan", "nested": {"chunks": 3}}
    await store.record_usage_event(
        metric="pages_embedded", value=3, event_id=eid, metadata=meta
    )
    row = await _fetch(storage, eid)
    raw = row[4]
    # Depending on the psycopg/SQLAlchemy JSONB codec in play, Postgres may
    # return JSONB as a Python dict or as a JSON str; SQLite stores TEXT.
    # Handle both so the test is robust across driver/codec versions.
    loaded = raw if isinstance(raw, dict) else json.loads(raw)
    assert loaded == meta


async def test_occurred_at_roundtrip(storage, monkeypatch):
    """occurred_at round-trips to the same instant on both backends.

    The store binds a datetime on Postgres and an ISO string on SQLite (the
    only dialect-specific branch in the store); this pins that both read back
    to the same instant regardless of the stored representation.
    """
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)
    eid = str(uuid.uuid4())
    when = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    await store.record_usage_event(
        metric="pages_embedded", value=1, event_id=eid, occurred_at=when
    )
    row = await _fetch(storage, eid)
    stored = row[1]
    # SQLite returns the ISO string we bound; Postgres returns a datetime.
    parsed = stored if isinstance(stored, datetime) else datetime.fromisoformat(stored)
    # Aware-datetime equality compares the instant, so a UTC value coming back
    # in another session tz still matches; a naive value (none expected) is
    # treated as UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    assert parsed == when


async def test_metadata_none_is_null(storage, monkeypatch):
    """Omitting metadata stores SQL NULL, not the string 'null'."""
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)
    eid = str(uuid.uuid4())
    await store.record_usage_event(
        metric="tokens_embedded", value=1, event_id=eid, metadata=None
    )
    row = await _fetch(storage, eid)
    assert row[4] is None


async def test_best_effort_swallows_db_errors(storage, monkeypatch, caplog):
    """A DB failure is logged + dropped, never raised into the caller."""
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)

    recorded: list[tuple] = []
    monkeypatch.setattr(
        store_module,
        "record_db_operation",
        lambda *args, **kwargs: recorded.append(args),
    )

    def _boom():
        raise RuntimeError("db down")

    # ``acquire`` raises on call — the store must catch and continue.
    monkeypatch.setattr(storage, "acquire", _boom)

    # Must not raise.
    with caplog.at_level(logging.WARNING, logger="nextcloud_mcp_server.usage.store"):
        await store.record_usage_event(metric="pages_embedded", value=1)

    assert recorded, "record_db_operation should be called on the error path"
    assert recorded[-1][3] == "error"
    # The observability contract: the dropped write surfaces at WARNING.
    assert any(
        r.levelno == logging.WARNING and "usage metering write dropped" in r.message
        for r in caplog.records
    )


async def test_best_effort_swallows_unserializable_metadata(
    storage, monkeypatch, caplog
):
    """Non-serializable metadata is swallowed, not raised into the caller.

    json.dumps runs inside the best-effort try, so a metadata value the JSON
    encoder can't handle must drop the event like any other write failure
    rather than surfacing to the user op.
    """
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)

    # An arbitrary object is not JSON-serializable; json.dumps raises TypeError.
    bad_metadata = {"obj": object()}

    # Must not raise.
    with caplog.at_level(logging.WARNING, logger="nextcloud_mcp_server.usage.store"):
        await store.record_usage_event(
            metric="pages_embedded", value=1, metadata=bad_metadata
        )

    # Nothing was written — the encode failed before the insert.
    assert await _count(storage) == 0
    # Silent data loss would be a footgun once metering is on: same WARNING
    # contract as the DB-error path.
    assert any(
        r.levelno == logging.WARNING and "usage metering write dropped" in r.message
        for r in caplog.records
    )


# --- Batch write: record_usage_events (Deck #667) --------------------------


async def test_batch_records_all_events(storage, monkeypatch):
    """A batch writes every event, with values and metadata intact."""
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)
    eids = [str(uuid.uuid4()) for _ in range(3)]
    await store.record_usage_events(
        [
            UsageEvent("tokens_embedded", 100, {"m": "a"}, event_id=eids[0]),
            UsageEvent("pages_embedded", 5, {"m": "a"}, event_id=eids[1]),
            UsageEvent("bytes_stored", 2048, {"m": "a"}, event_id=eids[2]),
        ]
    )
    assert await _count(storage) == 3
    by_metric = {}
    for eid in eids:
        row = await _fetch(storage, eid)
        assert row is not None
        by_metric[row[2]] = row[3]
    assert by_metric == {
        "tokens_embedded": 100,
        "pages_embedded": 5,
        "bytes_stored": 2048,
    }


async def test_batch_uses_single_connection(storage, monkeypatch, mocker):
    """The whole batch runs on ONE acquire()/transaction, not one per event."""
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)
    spy = mocker.spy(storage, "acquire")
    await store.record_usage_events(
        [
            UsageEvent("tokens_embedded", 1),
            UsageEvent("pages_embedded", 2),
            UsageEvent("bytes_stored", 3),
        ]
    )
    # One connection checkout for all three inserts — the point of Deck #667.
    assert spy.call_count == 1


async def test_batch_is_traced(storage, monkeypatch, mocker):
    """DB activity is traced: the batch opens a db.<dialect>.insert span."""
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)
    spy = mocker.spy(store_module, "trace_db_operation")
    await store.record_usage_events([UsageEvent("pages_embedded", 1)])
    spy.assert_called_once_with(storage.dialect, "insert", "usage_events")


async def test_batch_records_connect_duration_separately(storage, monkeypatch):
    """Connection acquisition is measured on its own, not folded into execute.

    Deck #678: one span/metric covering acquire+execute hid a ~600ms connect
    inside an apparently slow insert. The connect must be independently
    observable, or that class of regression is invisible in Prometheus.
    """
    _set_metering(monkeypatch, True)
    before = _connect_observations(storage.dialect)

    store = UsageEventStore(storage)
    await store.record_usage_events(
        [UsageEvent("tokens_embedded", 1), UsageEvent("pages_embedded", 2)]
    )

    # Exactly one connect for the whole batch: the acquire is amortized across
    # the events (Deck #667), and it is now attributable on its own.
    assert _connect_observations(storage.dialect) == pytest.approx(before + 1)


async def test_failed_connect_is_still_measured(storage, monkeypatch):
    """A connect that fails is recorded too — a slow timeout is the sample
    you most want to see, so it must not be dropped from the histogram."""
    _set_metering(monkeypatch, True)
    before = _connect_observations(storage.dialect)

    class _UnreachableEngine:
        async def connect(self):
            raise RuntimeError("connection refused")

    # ``AsyncEngine.connect`` is read-only, so swap the engine itself.
    monkeypatch.setattr(storage, "engine", _UnreachableEngine())

    store = UsageEventStore(storage)
    # Best-effort contract still holds: the failure must not reach the caller.
    await store.record_usage_events([UsageEvent("pages_embedded", 1)])

    assert _connect_observations(storage.dialect) == pytest.approx(before + 1)


async def test_batch_flag_off_is_noop(storage, monkeypatch, mocker):
    """Metering disabled → no rows and no DB access, even with events queued."""
    _set_metering(monkeypatch, False)
    store = UsageEventStore(storage)
    spy = mocker.spy(storage, "acquire")
    await store.record_usage_events([UsageEvent("pages_embedded", 1)])
    assert spy.call_count == 0
    assert await _count(storage) == 0


async def test_batch_empty_is_noop(storage, monkeypatch, mocker):
    """An empty event list touches the DB not at all."""
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)
    spy = mocker.spy(storage, "acquire")
    await store.record_usage_events([])
    assert spy.call_count == 0
    assert await _count(storage) == 0


async def test_batch_best_effort_swallows_encode_error(storage, monkeypatch, caplog):
    """A non-serializable event is swallowed (best-effort); the batch writes nothing."""
    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)
    with caplog.at_level(logging.WARNING, logger="nextcloud_mcp_server.usage.store"):
        await store.record_usage_events(
            [UsageEvent("pages_embedded", 1, metadata={"obj": object()})]
        )
    assert await _count(storage) == 0
    assert any(
        r.levelno == logging.WARNING and "usage metering batch dropped" in r.message
        for r in caplog.records
    )


async def test_batch_rolls_back_on_midbatch_failure(storage, monkeypatch):
    """A DB failure mid-batch rolls back the WHOLE document's events (atomic).

    Forces the 2nd of three inserts to raise: the first insert must not survive,
    proving the one-transaction batch is all-or-nothing (docstring claim), not a
    partial write.
    """
    from nextcloud_mcp_server.auth.storage import _DBConn

    _set_metering(monkeypatch, True)
    store = UsageEventStore(storage)

    real_execute = _DBConn.execute
    calls = {"n": 0}

    def flaky_execute(self, sql, params=()):
        calls["n"] += 1
        if calls["n"] == 2:  # blow up on the 2nd insert of the 3-event batch
            raise RuntimeError("boom mid-batch")
        return real_execute(self, sql, params)

    monkeypatch.setattr(_DBConn, "execute", flaky_execute)
    # Best-effort: the swallowed failure must not raise into the caller.
    await store.record_usage_events(
        [
            UsageEvent("tokens_embedded", 1),
            UsageEvent("pages_embedded", 2),
            UsageEvent("bytes_stored", 3),
        ]
    )
    monkeypatch.undo()  # restore the real execute before counting

    # The 1st insert never commits — the transaction rolls back on the failing
    # batch, so NONE of the document's events are persisted.
    assert await _count(storage) == 0
