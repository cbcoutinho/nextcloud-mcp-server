"""End-to-end Postgres smoke for the procrastinate ingest queue (Deck #183).

Validates the queue mechanics the in-memory connector can't: real
``queueing_lock`` partial-unique dedup, idempotent schema apply, and the
``list_queues`` stats the status surface reads. Opt-in like
``test_storage_postgres.py``::

    docker compose --profile postgres up -d postgres-test
    export TEST_DATABASE_URL=postgresql+asyncpg://mcp:mcp@localhost:5433/mcp
    uv run pytest tests/integration/test_ingest_queue_postgres.py -v -m postgres

Skipped when ``TEST_DATABASE_URL`` is unset or the service is unreachable.
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest

import nextcloud_mcp_server.config as config_module
from nextcloud_mcp_server.vector.queue.procrastinate import (
    INGEST_QUEUE_NAME,
    ProcrastinateTaskProducer,
    apply_ingest_queue_schema,
    build_app_for_url,
    get_ingest_job_counts,
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
            "TEST_DATABASE_URL=postgresql+asyncpg://mcp:mcp@localhost:5433/mcp"
        )
    # pytest.skip raises, but ty doesn't model it as NoReturn — narrow explicitly.
    assert url is not None
    if not _reachable(url):
        pytest.skip(f"Postgres at {url} is not reachable")
    return url


@pytest.fixture
async def fresh_app(postgres_url: str, monkeypatch: pytest.MonkeyPatch):
    """Drop+recreate the public schema, then apply procrastinate's schema."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()

    # build_app_for_url passes the URL explicitly to get_procrastinate_conninfo,
    # so only the ssl lookup (which reads settings) needs pinning here.
    monkeypatch.setattr(config_module, "get_database_ssl", lambda: None)

    app = build_app_for_url(postgres_url)
    await apply_ingest_queue_schema(app)
    return app


def _task(doc_id: str, doc_type: str = "note") -> DocumentTask:
    return DocumentTask(
        user_id="alice",
        doc_id=doc_id,
        doc_type=doc_type,
        operation="index",
        modified_at=100,
        etag=f"etag-{doc_id}",
    )


async def test_ingest_queue_end_to_end(fresh_app):
    """One self-contained smoke against real Postgres.

    Kept as a single test so each assertion runs against the same freshly-applied
    schema — splitting across functions reintroduces the inter-test ``DROP
    SCHEMA`` that confuses pooled psycopg connections' cached prepared statements
    (a test-harness artifact, not a production path: prod never drops the schema).
    """
    # 1. Schema is present and a second apply is a no-op (idempotent).
    await apply_ingest_queue_schema(fresh_app)

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
