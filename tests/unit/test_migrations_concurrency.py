"""Unit tests pinning the process-wide Alembic command lock.

Migrations run per MCP session (``app_lifespan_basic`` ->
``RefreshTokenStorage.initialize`` -> ``to_thread.run_sync(upgrade_database)``),
so a client that opens several sessions at once used to run
``command.upgrade`` from several worker threads simultaneously. Alembic's
``EnvironmentContext`` keeps its ``config``/``script`` proxies in *module
globals* on ``alembic.context``, so the concurrent runs clobbered each other:
the loser's ``env.py`` raised ``AttributeError: module 'alembic.context' has no
attribute 'config'`` and teardown then raised ``KeyError: 'script'``, crashing
the MCP session.

These tests prove the serialization in :mod:`nextcloud_mcp_server.migrations`
holds so that regression can't come back.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from nextcloud_mcp_server import migrations
from nextcloud_mcp_server.migrations import get_current_revision, upgrade_database

pytestmark = pytest.mark.unit

CONCURRENT_UPGRADES = 4


def test_concurrent_upgrades_do_not_corrupt_alembic_globals(tmp_path):
    """Several threads upgrading at once all succeed (GH: session crash)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'tokens.db'}"
    errors: list[Exception] = []
    start = threading.Barrier(CONCURRENT_UPGRADES)

    def run_upgrade() -> None:
        start.wait(timeout=30)
        try:
            upgrade_database(db_url, "head")
        except Exception as exc:
            # Recorded rather than raised: an exception in a worker thread
            # would otherwise only print a traceback and still pass the test.
            errors.append(exc)

    threads = [
        threading.Thread(target=run_upgrade, name=f"upgrade-{i}")
        for i in range(CONCURRENT_UPGRADES)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads), "upgrade thread hung"
    assert errors == [], f"concurrent upgrades failed: {errors!r}"

    # The schema is actually at head, not merely crash-free.
    assert get_current_revision(db_url) is not None
    with sqlite3.connect(tmp_path / "tokens.db") as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "refresh_tokens" in tables


def test_upgrade_holds_the_shared_alembic_lock(mocker):
    """``upgrade_database`` runs ``command.upgrade`` under the module lock.

    Guards the specific mistake of dropping the ``with`` while keeping the
    lock object around — which would look fine but restore the race.
    """
    held: list[bool] = []

    def record_lock_state(config: object, revision: str) -> None:
        held.append(migrations._ALEMBIC_COMMAND_LOCK.locked())

    mocker.patch.object(migrations.command, "upgrade", side_effect=record_lock_state)

    upgrade_database("sqlite+aiosqlite:///:memory:", "head")

    assert held == [True]
    assert not migrations._ALEMBIC_COMMAND_LOCK.locked(), "lock leaked after upgrade"
