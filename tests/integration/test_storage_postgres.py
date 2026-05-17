"""End-to-end Postgres backend smoke for RefreshTokenStorage (ADR-026).

Exercises every storage method touched by the SQLAlchemy / asyncpg port
against a fresh Postgres schema. The test is opt-in: it requires the
``postgres-test`` docker-compose service to be running and
``TEST_DATABASE_URL`` to be exported.

Bring up the dependency once::

    docker compose --profile postgres up -d postgres-test
    export TEST_DATABASE_URL=postgresql+asyncpg://mcp:mcp@localhost:5433/mcp

Then run::

    uv run pytest tests/integration/test_storage_postgres.py -v -m postgres

When ``TEST_DATABASE_URL`` is unset (or the service is unreachable) the
test is skipped so the full suite still passes locally without Docker.
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

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
    if not _reachable(url):
        pytest.skip(f"Postgres at {url} is not reachable")
    return url


@pytest.fixture
async def reset_schema(postgres_url: str):
    """Drop+recreate the public schema before and after each test."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _reset() -> None:
        engine = create_async_engine(postgres_url, future=True)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP SCHEMA public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
        finally:
            await engine.dispose()

    await _reset()
    yield
    await _reset()


@pytest.fixture
async def storage(postgres_url: str, reset_schema):
    key = Fernet.generate_key()
    s = RefreshTokenStorage(database_url=postgres_url, encryption_key=key)
    await s.initialize()
    yield s


async def test_refresh_token_roundtrip(storage: RefreshTokenStorage):
    """Store + retrieve + upsert + delete a refresh token end-to-end."""
    await storage.store_refresh_token(
        user_id="alice", refresh_token="rt-1", expires_at=9_999_999_999
    )
    tok = await storage.get_refresh_token("alice")
    assert tok is not None
    assert tok["refresh_token"] == "rt-1"
    assert tok["expires_at"] == 9_999_999_999

    # Upsert preserves user_id, swaps token contents.
    await storage.store_refresh_token(
        user_id="alice", refresh_token="rt-2", expires_at=9_999_999_999
    )
    tok = await storage.get_refresh_token("alice")
    assert tok is not None and tok["refresh_token"] == "rt-2"

    assert await storage.delete_refresh_token("alice") is True
    assert await storage.get_refresh_token("alice") is None


async def test_app_password_roundtrip(storage: RefreshTokenStorage):
    """Store + retrieve + replace + delete a scoped app password.

    The ``app_password=`` keyword-arg literals are bound to local
    variables so the bare ``# NOSONAR`` marker can anchor to the same
    physical line as the literal — SonarQube's hard-coded-credential
    heuristic ignores the marker otherwise. These are localhost test
    fixtures with no production reach.
    """
    bob_pw_v1 = "pw-1"  # NOSONAR
    await storage.store_app_password(user_id="bob", app_password=bob_pw_v1)
    assert await storage.get_app_password("bob") == bob_pw_v1

    # Replace path exercises the ON CONFLICT DO UPDATE on the singleton row.
    bob_pw_v2 = "pw-2"  # NOSONAR
    await storage.store_app_password(user_id="bob", app_password=bob_pw_v2)
    assert await storage.get_app_password("bob") == bob_pw_v2

    assert await storage.delete_app_password("bob") is True
    assert await storage.get_app_password("bob") is None


async def test_oauth_session_lifecycle(storage: RefreshTokenStorage):
    """Cover the ADR-004 progressive-consent session table."""
    await storage.store_oauth_session(
        session_id="sess-1",
        client_redirect_uri="http://localhost:12345/callback",
        mcp_authorization_code="mcp-code-abc",
        flow_type="hybrid",
        ttl_seconds=600,
    )
    fetched = await storage.get_oauth_session("sess-1")
    assert fetched is not None
    assert fetched["mcp_authorization_code"] == "mcp-code-abc"

    by_code = await storage.get_oauth_session_by_mcp_code("mcp-code-abc")
    assert by_code is not None and by_code["session_id"] == "sess-1"


async def test_webhook_tracking(storage: RefreshTokenStorage):
    """Tracks webhook ↔ preset mappings via ON CONFLICT upserts."""
    await storage.store_webhook(webhook_id=101, preset_id="notes_sync")
    await storage.store_webhook(webhook_id=202, preset_id="notes_sync")
    await storage.store_webhook(webhook_id=303, preset_id="calendar_sync")

    assert sorted(await storage.get_webhooks_by_preset("notes_sync")) == [101, 202]
    assert await storage.get_webhooks_by_preset("calendar_sync") == [303]

    # Re-storing the same webhook_id is a no-op upsert.
    await storage.store_webhook(webhook_id=101, preset_id="notes_sync")
    assert sorted(await storage.get_webhooks_by_preset("notes_sync")) == [101, 202]

    assert await storage.delete_webhook(webhook_id=101) is True
    assert await storage.get_webhooks_by_preset("notes_sync") == [202]


async def test_audit_log_capture(storage: RefreshTokenStorage):
    """Audit events from upstream methods land in audit_logs."""
    carol_pw = "x"  # NOSONAR
    await storage.store_app_password(user_id="carol", app_password=carol_pw)
    logs = await storage.get_audit_logs(user_id="carol", limit=10)
    assert any(entry["event"] == "store_app_password" for entry in logs)


async def test_cleanup_expired_roundtrip(storage: RefreshTokenStorage):
    """``cleanup_expired_*`` paths rely on DELETE rowcount across dialects.

    Regression guard for the bot review on PR #798 — the original
    integration tests didn't exercise these methods, which historically
    have been a source of dialect-portability bugs.
    """
    # Insert one fresh + one expired refresh token.
    await storage.store_refresh_token(
        user_id="fresh-user", refresh_token="fresh", expires_at=9_999_999_999
    )
    await storage.store_refresh_token(
        user_id="expired-user", refresh_token="stale", expires_at=1
    )

    # Insert one fresh + one expired OAuth session.
    await storage.store_oauth_session(
        session_id="sess-fresh",
        client_redirect_uri="http://localhost/cb",
        mcp_authorization_code="code-fresh",
        ttl_seconds=600,
    )
    await storage.store_oauth_session(
        session_id="sess-stale",
        client_redirect_uri="http://localhost/cb",
        mcp_authorization_code="code-stale",
        ttl_seconds=-3600,  # expires_at = now - 1h
    )

    # Insert one fresh + one expired browser session.
    await storage.create_browser_session(
        session_id="bs-fresh", user_id="alice", ttl_seconds=600
    )
    await storage.create_browser_session(
        session_id="bs-stale", user_id="alice", ttl_seconds=-3600
    )

    tokens_deleted = await storage.cleanup_expired_tokens()
    sessions_deleted = await storage.cleanup_expired_sessions()
    browser_deleted = await storage.cleanup_expired_browser_sessions()

    assert tokens_deleted == 1, f"expected 1 expired token, got {tokens_deleted}"
    assert sessions_deleted == 1, (
        f"expected 1 expired oauth session, got {sessions_deleted}"
    )
    assert browser_deleted == 1, (
        f"expected 1 expired browser session, got {browser_deleted}"
    )

    # Fresh rows survived.
    assert await storage.get_refresh_token("fresh-user") is not None
    assert await storage.get_refresh_token("expired-user") is None
    assert await storage.get_oauth_session("sess-fresh") is not None
    assert await storage.get_oauth_session("sess-stale") is None


async def test_browser_session_delete_returning(storage: RefreshTokenStorage):
    """Exercise the ``DELETE … RETURNING user_id`` path on Postgres.

    ``delete_browser_session`` is the only RETURNING clause in the
    storage layer and the most dialect-sensitive SQL in this PR — it
    needed SQLite ≥ 3.35 specifically because of RETURNING. Bot review
    on PR #798 round 2 flagged that the existing cleanup test didn't
    actually exercise this path. Asserts both the present and absent
    cases so the asyncpg result-handling for RETURNING is covered.
    """
    await storage.create_browser_session(
        session_id="bs-returning", user_id="alice", ttl_seconds=600
    )
    assert await storage.get_browser_session_user("bs-returning") == "alice"

    assert await storage.delete_browser_session("bs-returning") is True
    assert await storage.get_browser_session_user("bs-returning") is None

    # Deleting a nonexistent session returns False (RETURNING yields no
    # row → rowcount path).
    assert await storage.delete_browser_session("never-existed") is False


async def test_close_disposes_engine(postgres_url: str, reset_schema):
    """``close()`` releases pooled asyncpg connections and is idempotent.

    PR #798 round-4 review (bot #4): the engine wasn't being disposed on
    shutdown, leaking server-side connection slots until the Postgres
    idle-in-transaction timeout fired. This test confirms ``close()``
    nulls the engine, leaves the storage in a non-initialized state,
    and a second ``close()`` call is a no-op rather than an exception.
    """
    s = RefreshTokenStorage(
        database_url=postgres_url, encryption_key=Fernet.generate_key()
    )
    await s.initialize()
    assert s.engine is not None
    assert s._initialized is True

    await s.close()
    assert s.engine is None
    assert s._initialized is False

    # Idempotent — second close is a no-op, no AttributeError.
    await s.close()
    assert s.engine is None


async def test_concurrent_initialize_serialized_by_advisory_lock(
    postgres_url: str, reset_schema
):
    """Concurrent pod startup must serialize on pg_advisory_lock.

    PR #798 round-4 review (bot #3): without a migration lock, two
    pods racing the rolling-update can both detect ``has_alembic=False``
    and both run ``upgrade_database(URL, "head")``; the second crashes
    with "relation already exists". This test spawns three concurrent
    ``RefreshTokenStorage.initialize()`` calls against a fresh schema
    and asserts all of them complete successfully (the advisory lock
    serializes them; the second/third observe ``has_alembic=True``
    after the first commits and take the upgrade fast-path).
    """
    import anyio

    async def init_one() -> None:
        s = RefreshTokenStorage(
            database_url=postgres_url, encryption_key=Fernet.generate_key()
        )
        try:
            await s.initialize()
        finally:
            await s.close()

    # No exception = serialization worked. Without the lock, this
    # raised ``relation "refresh_tokens" already exists`` on the second
    # task in CI runs prior to this fix.
    async with anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(init_one)

    # Verify the schema actually landed once, not three times: the
    # alembic_version table should exist with one row at the head revision.
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url, future=True)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT count(*) FROM alembic_version"))
            (count,) = result.fetchone()
            assert count == 1, f"expected 1 alembic_version row, got {count}"
    finally:
        await engine.dispose()
