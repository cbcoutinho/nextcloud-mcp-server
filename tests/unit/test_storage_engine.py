"""Unit tests for ``RefreshTokenStorage._build_postgres_engine``.

PR #799 switched the Postgres engine from ``AsyncAdaptedQueuePool``
to ``NullPool`` to eliminate cross-event-loop crashes under anyio
TaskGroups. The method is factored out explicitly so a future
engine-arg unit test has a single seam to mock — these tests pin
the pool class and the connect-args plumbing so a refactor can't
silently regress to a sharing pool.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit


def _storage(url: str) -> RefreshTokenStorage:
    # ``encryption_key=None`` is fine for engine-shape tests; the
    # cipher is only constructed lazily for cipher-protected ops,
    # and these tests never call those.
    return RefreshTokenStorage(database_url=url, encryption_key=None)


# The unit-test environment may not have the optional ``[postgres]``
# extra installed (asyncpg is the C-extension dep). Skip the
# engine-construction tests when asyncpg isn't importable rather
# than hitting the "DATABASE_URL points at Postgres via asyncpg but
# the 'asyncpg' driver is not installed" guard — that branch is
# exercised explicitly by ``test_postgres_engine_missing_asyncpg_driver_message``
# below.
asyncpg_required = pytest.importorskip("asyncpg")


def test_postgres_engine_uses_nullpool():
    """The Postgres engine must use ``NullPool`` to avoid cross-loop
    crashes under anyio TaskGroups (see PR #799)."""
    storage = _storage("postgresql+asyncpg://mcp:placeholder@db.example.com:5432/mcp")
    engine = storage._build_postgres_engine()

    assert isinstance(engine, AsyncEngine)
    # ``engine.pool`` is the sync proxy pool; the underlying pool
    # class is what we care about for the loop-binding behaviour.
    assert isinstance(engine.pool, NullPool), (
        f"expected NullPool, got {type(engine.pool).__name__} — a regression "
        "to QueuePool/SingletonThreadPool will re-introduce the cross-event-"
        "loop crashes from PR #799"
    )


def test_postgres_engine_ignores_pool_sizing_settings(monkeypatch: pytest.MonkeyPatch):
    """``DATABASE_POOL_SIZE`` / ``DATABASE_MAX_OVERFLOW`` are kept as
    deprecated no-ops for backward compat. NullPool has no concept of
    these, so changing them must not raise or change pool type."""
    # Stash arbitrarily large values into the settings the engine
    # consults; NullPool is parameterless so the engine should ignore
    # them entirely.
    from nextcloud_mcp_server import config as cfg

    monkeypatch.setattr(cfg.get_settings(), "database_pool_size", 99, raising=False)
    monkeypatch.setattr(cfg.get_settings(), "database_max_overflow", 99, raising=False)

    storage = _storage("postgresql+asyncpg://mcp:placeholder@db.example.com:5432/mcp")
    engine = storage._build_postgres_engine()
    assert isinstance(engine.pool, NullPool)


def test_postgres_engine_missing_asyncpg_driver_message(
    monkeypatch: pytest.MonkeyPatch,
):
    """When the ``+asyncpg`` dialect is requested but the asyncpg
    optional dep isn't installed, the engine builder must surface an
    actionable error before SQLAlchemy emits its generic
    ``ModuleNotFoundError``."""
    import importlib.util

    def _fake_find_spec(name: str):
        return None if name == "asyncpg" else importlib.util.find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)

    storage = _storage("postgresql+asyncpg://mcp:placeholder@db.example.com:5432/mcp")
    with pytest.raises(RuntimeError, match="asyncpg.*not installed"):
        storage._build_postgres_engine()
