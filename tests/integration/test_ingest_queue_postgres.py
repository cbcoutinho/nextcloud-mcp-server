"""End-to-end Postgres smoke for the procrastinate ingest queue (Deck #183).

Validates the queue mechanics the in-memory connector can't: real
``queueing_lock`` partial-unique dedup, idempotent schema apply, and the
``list_queues`` stats the status surface reads. Opt-in like
``test_storage_postgres.py``::

    docker compose --profile postgres up -d postgres-test
    export TEST_DATABASE_URL=postgresql+psycopg://mcp:mcp@localhost:5433/mcp
    uv run pytest tests/integration/test_ingest_queue_postgres.py -v -m postgres

Skipped when ``TEST_DATABASE_URL`` is unset or the service is unreachable.
"""

from __future__ import annotations

import os
import socket
import types
from typing import cast
from urllib.parse import urlparse

import pytest
from procrastinate import JobContext

import nextcloud_mcp_server.vector.queue.procrastinate as pq
from nextcloud_mcp_server.migrations import upgrade_database
from nextcloud_mcp_server.vector.queue.procrastinate import (
    INGEST_QUEUE_NAME,
    ProcrastinateTaskProducer,
    build_app_for_url,
    get_ingest_job_counts,
    reclaim_stalled_ingest_jobs,
)
from nextcloud_mcp_server.vector.scanner import DocumentTask

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def _postgres_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or None


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 5432), timeout=1.0
        ):
            return True
    except OSError:
        return False


@pytest.fixture
def postgres_url() -> str:
    url = _postgres_url()
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL not set — run "
            "`docker compose --profile postgres up -d postgres-test` and export "
            "TEST_DATABASE_URL=postgresql+psycopg://mcp:mcp@localhost:5433/mcp"
        )
    # pytest.skip raises, but ty doesn't model it as NoReturn — narrow explicitly.
    assert url is not None
    if not _reachable(url):
        pytest.skip(f"Postgres at {url} is not reachable")
    return url


@pytest.fixture
async def fresh_app(postgres_url: str):
    """Drop+recreate the public schema, then migrate it (procrastinate included)."""
    from anyio import to_thread
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()

    # get_procrastinate_conninfo passes the URL through verbatim (only strips
    # the +psycopg driver tag), so there is no settings/ssl lookup to pin.
    await to_thread.run_sync(upgrade_database, postgres_url)
    return build_app_for_url(postgres_url)


def _task(doc_id: str, doc_type: str = "note") -> DocumentTask:
    return DocumentTask(
        user_id="alice",
        doc_id=doc_id,
        doc_type=doc_type,
        operation="index",
        modified_at=100,
        etag=f"etag-{doc_id}",
    )


async def test_ingest_queue_end_to_end(fresh_app, postgres_url):
    """One self-contained smoke against real Postgres.

    Kept as a single test so each assertion runs against the same freshly-applied
    schema — splitting across functions reintroduces the inter-test ``DROP
    SCHEMA`` that confuses pooled psycopg connections' cached prepared statements
    (a test-harness artifact, not a production path: prod never drops the schema).
    """
    # 1. Schema is present and a second migration is a no-op (idempotent).
    from anyio import to_thread

    await to_thread.run_sync(upgrade_database, postgres_url)

    async with fresh_app.open_async():
        present = await fresh_app.connector.execute_query_one_async(
            "SELECT to_regclass('procrastinate_jobs') IS NOT NULL AS present"
        )
        assert present["present"] is True

        # 2. Defer + real queueing_lock dedup (one todo per doc).
        producer = ProcrastinateTaskProducer(fresh_app)
        await producer.send(_task("1"))
        await producer.send(_task("1"))  # deduped by queueing_lock
        await producer.send(_task("2"))

        rows = await fresh_app.connector.execute_query_all_async(
            "SELECT count(*) AS n FROM procrastinate_jobs "
            "WHERE queue_name = %(q)s AND status = 'todo'",
            q=INGEST_QUEUE_NAME,
        )
        assert rows[0]["n"] == 2

        # 3. The status-surface counts read agrees.
        counts = await get_ingest_job_counts(fresh_app)
        assert counts.get("todo") == 2

        # 4. Fresh todo jobs are not "doing", so none are stalled.
        stalled = await fresh_app.job_manager.get_stalled_jobs(
            queue=INGEST_QUEUE_NAME, seconds_since_heartbeat=0
        )
        assert list(stalled) == []


async def test_reclaim_discards_real_queueing_lock_collision(fresh_app, monkeypatch):
    """The reclaim fix, end-to-end against real Postgres (PR #999).

    Confirms the exact production path the unit test can only simulate: that
    ``retry_job_by_id_async``'s UPDATE raises ``procrastinate.exceptions.
    UniqueViolation`` (not a bare ``psycopg`` error that would slip past the
    ``except UniqueViolation`` branch) when a reclaimed orphan collides with a
    live ``todo`` sibling — and that the orphan is then discarded, not stranded.
    """
    # Threshold 0 so the manually-orphaned ``doing`` job is immediately stalled;
    # reclaim only reads these two settings.
    monkeypatch.setattr(
        pq,
        "get_settings",
        lambda: types.SimpleNamespace(
            ingest_stalled_job_seconds=0,
            ingest_doing_max_seconds=0,
            ingest_reclaim_retry_delay_seconds=0,
        ),
    )

    async with fresh_app.open_async():
        producer = ProcrastinateTaskProducer(fresh_app)

        # Job A for doc "1" → todo, then orphan it into ``doing`` (worker crashed
        # mid-job) so its queueing_lock no longer occupies the todo partial-index.
        await producer.send(_task("1"))
        await fresh_app.connector.execute_query_async(
            "UPDATE procrastinate_jobs SET status = 'doing' WHERE queue_name = %(q)s",
            q=INGEST_QUEUE_NAME,
        )
        # The scanner re-queues the same doc → fresh todo sibling B holding the
        # identical queueing_lock (allowed: A is 'doing', not 'todo').
        await producer.send(_task("1"))

        by_status = {
            r["status"]: r["n"]
            for r in await fresh_app.connector.execute_query_all_async(
                "SELECT status, count(*) AS n FROM procrastinate_jobs GROUP BY status"
            )
        }
        assert by_status == {"doing": 1, "todo": 1}  # A doing + B todo, same lock

        # Reclaim: retry(A) → 'todo' collides with B → UniqueViolation → A is
        # discarded (deleted). Must not raise, and must leave exactly B as todo.
        ctx = cast(JobContext, types.SimpleNamespace(app=fresh_app))
        await reclaim_stalled_ingest_jobs(ctx, timestamp=0)

        by_status = {
            r["status"]: r["n"]
            for r in await fresh_app.connector.execute_query_all_async(
                "SELECT status, count(*) AS n FROM procrastinate_jobs GROUP BY status"
            )
        }
        assert by_status == {"todo": 1}  # orphan gone; the live sibling survives


async def test_reclaim_backstops_live_worker_doing_strand(fresh_app, monkeypatch):
    """Reclaim a job stranded in ``doing`` under a LIVE worker.

    The heartbeat sweep only catches DEAD/pruned workers. When a job's OWN
    completion crashes — e.g. procrastinate's ``doing``->``todo`` retry raises an
    unhandled ``queueing_lock`` UniqueViolation against a scanner-deferred ``todo``
    sibling — the row is left ``doing`` while its worker stays alive and
    heart-beating, so ``select_stalled_jobs_by_heartbeat`` never returns it and the
    job would sit forever (observed: 2 docs pinned ``doing``, reaper reclaimed=0).
    The time-in-``doing`` backstop (``ingest_doing_max_seconds`` / by-started) must
    reclaim it. Regression for the ingest doing-strand fix.
    """
    # Heartbeat threshold huge so the heartbeat sweep can NEVER be what catches the
    # orphan; doing_max 0 so only the by-started backstop can.
    monkeypatch.setattr(
        pq,
        "get_settings",
        lambda: types.SimpleNamespace(
            ingest_stalled_job_seconds=10**9,
            ingest_doing_max_seconds=0,
            ingest_reclaim_retry_delay_seconds=0,
        ),
    )

    async with fresh_app.open_async():
        producer = ProcrastinateTaskProducer(fresh_app)

        # A LIVE worker (fresh heartbeat) — the orphan is owned by it, not a dead one.
        worker = await fresh_app.connector.execute_query_one_async(
            "INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id"
        )
        worker_id = worker["id"]

        # Job A → todo, then strand it in ``doing`` OWNED BY THE LIVE WORKER, with a
        # ``started`` event an hour in the past (its crashing attempt began long ago).
        await producer.send(_task("1"))
        job_a = await fresh_app.connector.execute_query_one_async(
            "UPDATE procrastinate_jobs SET status='doing', worker_id=%(w)s "
            "WHERE queue_name=%(q)s RETURNING id",
            w=worker_id,
            q=INGEST_QUEUE_NAME,
        )
        await fresh_app.connector.execute_query_async(
            "INSERT INTO procrastinate_events (job_id, type, at) "
            "VALUES (%(jid)s, 'started', NOW() - INTERVAL '1 hour')",
            jid=job_a["id"],
        )
        # Scanner re-queues the doc → live todo sibling B, identical queueing_lock.
        await producer.send(_task("1"))

        # The heartbeat sweep is blind to it (worker alive, worker_id not null) — this
        # is the exact gap the backstop closes.
        hb = await fresh_app.job_manager.get_stalled_jobs(seconds_since_heartbeat=10**9)
        assert list(hb) == []

        ctx = cast(JobContext, types.SimpleNamespace(app=fresh_app))
        await reclaim_stalled_ingest_jobs(ctx, timestamp=0)

        by_status = {
            r["status"]: r["n"]
            for r in await fresh_app.connector.execute_query_all_async(
                "SELECT status, count(*) AS n FROM procrastinate_jobs GROUP BY status"
            )
        }
        # Backstop found A (by time-in-doing), retried it → collided with B →
        # discarded the redundant orphan; the live sibling survives to run.
        assert by_status == {"todo": 1}


def _fetch_job_skips_null_lock(postgres_url: str) -> bool:
    """True when procrastinate_fetch_job_v2 carries the 3.11 lock-less fast path."""
    from sqlalchemy import create_engine, text

    engine = create_engine(postgres_url)
    try:
        with engine.connect() as conn:
            body = conn.execute(
                text(
                    "SELECT pg_get_functiondef("
                    "'procrastinate_fetch_job_v2(character varying[], bigint)'"
                    "::regprocedure)"
                )
            ).scalar_one()
    finally:
        engine.dispose()
    return "jobs.lock IS NULL" in body


async def test_alembic_011_upgrades_existing_ingest_queue(fresh_app, postgres_url):
    """Revision 011 brings an existing queue's fetch function up to 3.11.

    Downgrading to 010 first puts the fetch function in the state of a queue
    created on 3.3-3.10.
    """
    from anyio import to_thread

    from nextcloud_mcp_server.migrations import downgrade_database

    assert _fetch_job_skips_null_lock(postgres_url)

    await to_thread.run_sync(downgrade_database, postgres_url, "010")
    assert not _fetch_job_skips_null_lock(postgres_url)

    await to_thread.run_sync(upgrade_database, postgres_url, "head")
    assert _fetch_job_skips_null_lock(postgres_url)

    # The replaced function still dispatches work.
    async with fresh_app.open_async():
        await ProcrastinateTaskProducer(fresh_app).send(_task("1"))
        job = await fresh_app.connector.execute_query_one_async(
            "SELECT id FROM procrastinate_fetch_job_v2(%(q)s, NULL)",
            q=[INGEST_QUEUE_NAME],
        )
        assert job["id"] is not None


async def test_alembic_011_skips_postgres_without_ingest_queue(postgres_url):
    """With no procrastinate schema yet (fresh install), 011 is a no-op.

    Upgrading to an explicit revision migrates only our own chain, so the queue
    stays absent.
    """
    from alembic.script import ScriptDirectory
    from anyio import to_thread
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from nextcloud_mcp_server.migrations import (
        get_alembic_config,
        get_current_revision,
    )

    engine = create_async_engine(postgres_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()

    await to_thread.run_sync(upgrade_database, postgres_url, "011")

    # The chain ran through 011 (skipping its SQL) to the current head.
    head = ScriptDirectory.from_config(
        get_alembic_config(postgres_url)
    ).get_current_head()
    assert get_current_revision(postgres_url) == head
    engine = create_async_engine(postgres_url, future=True)
    try:
        async with engine.connect() as conn:
            absent = await conn.scalar(
                text(
                    "SELECT to_regprocedure('procrastinate_fetch_job_v2(character varying[], bigint)') IS NULL"
                )
            )
    finally:
        await engine.dispose()
    assert absent


def _queue_versions(postgres_url: str) -> tuple[list[str], list[str], bool]:
    """Our revisions, procrastinate's revisions, and whether 3.12's lock_mode exists."""
    from sqlalchemy import create_engine, text

    from nextcloud_mcp_server.migrations import PROCRASTINATE_VERSION_TABLE

    engine = create_engine(postgres_url)
    try:
        with engine.connect() as conn:
            ours = list(conn.scalars(text("SELECT version_num FROM alembic_version")))
            theirs = list(
                conn.scalars(
                    text(f"SELECT version_num FROM {PROCRASTINATE_VERSION_TABLE}")
                )
            )
            lock_mode = conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'procrastinate_jobs'"
                    " AND column_name = 'lock_mode')"
                )
            )
    finally:
        engine.dispose()
    return ours, theirs, bool(lock_mode)


async def test_fresh_database_gets_procrastinate_pre_schema(fresh_app, postgres_url):
    """A fresh database is migrated up to the pinned ``pre`` revision only.

    procrastinate's revisions live in their own version table, so
    ``alembic_version`` holds just our head, as releases before the adoption expect.
    """
    from alembic.script import ScriptDirectory

    from nextcloud_mcp_server.migrations import (
        PROCRASTINATE_REVISION,
        get_alembic_config,
    )

    head = ScriptDirectory.from_config(
        get_alembic_config(postgres_url)
    ).get_current_head()
    assert _queue_versions(postgres_url) == ([head], [PROCRASTINATE_REVISION], True)


async def test_existing_ingest_queue_is_stamped_then_upgraded(postgres_url):
    """A queue created before the adoption is stamped, then gets 3.12's ``pre`` revisions.

    Such a queue has a procrastinate <= 3.10 schema plus 03.11.00_01 (our 011),
    i.e. ``procrastinate_0038``, and no procrastinate version table. Also checks
    that code from before the adoption can still run its own upgrade afterwards
    (a rollback), since our ``alembic_version`` holds only revisions it knows.
    """
    from anyio import to_thread
    from sqlalchemy import create_engine, text

    from alembic import command
    from nextcloud_mcp_server.migrations import (
        PROCRASTINATE_ADOPTED_AT,
        PROCRASTINATE_REVISION,
        PROCRASTINATE_VERSION_TABLE,
        _procrastinate_versions,
        get_alembic_config,
    )

    engine = create_engine(postgres_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()

    def build_pre_adoption_queue() -> None:
        procrastinate = get_alembic_config(postgres_url)
        versions = _procrastinate_versions(postgres_url)
        assert versions is not None
        procrastinate.set_main_option("version_locations", versions)
        procrastinate.set_main_option("version_table", PROCRASTINATE_VERSION_TABLE)
        command.upgrade(procrastinate, PROCRASTINATE_ADOPTED_AT)
        drop = create_engine(postgres_url)
        try:
            with drop.begin() as conn:
                conn.execute(text(f"DROP TABLE {PROCRASTINATE_VERSION_TABLE}"))
        finally:
            drop.dispose()
        command.upgrade(get_alembic_config(postgres_url), "head")  # ours only

    await to_thread.run_sync(build_pre_adoption_queue)

    await to_thread.run_sync(upgrade_database, postgres_url)
    ours, theirs, lock_mode = _queue_versions(postgres_url)
    assert theirs == [PROCRASTINATE_REVISION]
    assert lock_mode

    # Pre-adoption code: our chain only, against alembic_version only.
    await to_thread.run_sync(command.upgrade, get_alembic_config(postgres_url), "head")
    assert _queue_versions(postgres_url) == (ours, theirs, lock_mode)

    # 3.12 defers and fetches on the migrated schema.
    app = build_app_for_url(postgres_url)
    async with app.open_async():
        await ProcrastinateTaskProducer(app).send(_task("1"))
        job = await app.connector.execute_query_one_async(
            "SELECT id FROM procrastinate_fetch_job_v2(%(q)s, NULL)",
            q=[INGEST_QUEUE_NAME],
        )
        assert job["id"] is not None
