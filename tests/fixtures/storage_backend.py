"""Shared pytest fixture for parametrizing storage tests over backends.

Tests for ``RefreshTokenStorage`` are exercised against every backend that is
available in the current environment:

- ``sqlite`` — always available; uses a per-test tempfile.
- ``postgres`` — opt-in. Bring up the test instance with::

      docker compose --profile postgres up -d postgres-test

  and export the URL so the fixture picks it up::

      export TEST_DATABASE_URL=postgresql+psycopg://mcp:mcp@localhost:5433/mcp

  When ``TEST_DATABASE_URL`` is unset (or the host is unreachable), the
  Postgres parametrization is skipped automatically so the suite still runs
  cleanly without Docker.

Each Postgres test runs against an isolated schema that is dropped and
recreated between tests, mirroring the per-tempfile isolation that the
SQLite path gets for free.
"""

from __future__ import annotations

import os
from typing import Any

import pytest


def _postgres_url() -> str | None:
    """Resolve the Postgres URL for tests, or ``None`` when opted out."""
    return os.environ.get("TEST_DATABASE_URL") or None


def _postgres_reachable(url: str) -> bool:
    """Return ``True`` if the configured Postgres accepts TCP connections.

    A lightweight socket probe is used rather than a full DB handshake so
    we don't have to open a sync Postgres connection just for the test
    gate — the async psycopg engine only works inside an event loop.
    """
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _backend_params() -> list[Any]:
    """Build the pytest parametrize list, gating Postgres on availability."""
    params: list[Any] = [pytest.param("sqlite", id="sqlite")]
    url = _postgres_url()
    if url and _postgres_reachable(url):
        params.append(pytest.param(url, id="postgres"))
    return params


@pytest.fixture(params=_backend_params())
def storage_backend(request):
    """Yield ``{"kind": ..., "url": ..., "reset": <async>}`` per backend.

    For ``sqlite`` the test fixture builds its own tempfile path; only the
    ``kind`` discriminator is used. For ``postgres`` the URL is forwarded
    and a ``reset()`` coroutine is provided so test fixtures can wipe the
    schema between parametrized runs.
    """
    if request.param == "sqlite":
        yield {"kind": "sqlite"}
        return

    url = request.param

    async def reset() -> None:
        # Use a fresh async engine so we don't fight an async connection
        # the test might still be holding open at teardown time. psycopg3
        # is the only driver we ship for Postgres, so the reset path stays
        # event-loop-only.
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(url, future=True)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP SCHEMA public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
        finally:
            await engine.dispose()

    yield {"kind": "postgres", "url": url, "reset": reset}
