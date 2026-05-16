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
    """Store + retrieve + replace + delete a scoped app password."""
    await storage.store_app_password(user_id="bob", app_password="pw-1")
    assert await storage.get_app_password("bob") == "pw-1"

    # Replace path exercises the ON CONFLICT DO UPDATE on the singleton row.
    await storage.store_app_password(user_id="bob", app_password="pw-2")
    assert await storage.get_app_password("bob") == "pw-2"

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
    await storage.store_app_password(user_id="carol", app_password="x")
    logs = await storage.get_audit_logs(user_id="carol", limit=10)
    assert any(entry["event"] == "store_app_password" for entry in logs)
