"""Database migration utilities for nextcloud-mcp-server.

This module provides helper functions for managing Alembic database migrations
programmatically. It enables automatic migration on application startup and
provides CLI integration.

All helpers accept a SQLAlchemy URL (``sqlite+aiosqlite:///...`` or
``postgresql+psycopg://...``). When called without an explicit URL they fall
back to :func:`nextcloud_mcp_server.config.get_database_url`.
"""

import logging
import threading
from importlib import resources
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

import nextcloud_mcp_server.alembic as alembic_package
from alembic import command
from nextcloud_mcp_server.config import (
    get_database_url,
    is_sqlite_url,
    mask_db_password,
)

logger = logging.getLogger(__name__)

# Alembic's EnvironmentContext is NOT re-entrant across threads: entering it
# installs the ``config``/``script`` proxies as *module globals* on
# ``alembic.context`` (``alembic.util.langhelpers._install_proxy``) and exiting
# ``del``s them again. Two threads running a command concurrently therefore
# clobber each other's globals — the loser's ``env.py`` blows up with
# ``AttributeError: module 'alembic.context' has no attribute 'config'`` and the
# teardown then raises ``KeyError: 'script'``.
#
# This bites us because migrations run per MCP session (``app_lifespan_basic``
# -> ``RefreshTokenStorage.initialize`` -> ``to_thread.run_sync(upgrade_database)``),
# so a client opening several sessions at once crashes those sessions. The
# Postgres advisory lock in ``RefreshTokenStorage._migration_lock`` only covers
# cross-pod races and is a no-op on SQLite, so the serialization has to live
# here, next to the shared global state it protects.
#
# A ``threading.Lock`` (not ``anyio.Lock``, despite the repo default) is the
# right primitive: these are blocking calls made from worker threads, possibly
# under different event loops, and the lock is only ever held inside the worker
# thread — never on an event loop.
_ALEMBIC_COMMAND_LOCK = threading.Lock()

# procrastinate's schema is managed by the Alembic revisions it ships
# (procrastinate[alembic], Deck #1494). They are tracked in their own version
# table, so ``alembic_version`` keeps holding only our revisions: releases that
# predate this, still running during a rolling update or after a rollback, only
# find revisions they know there.
PROCRASTINATE_VERSION_TABLE = "alembic_version_procrastinate"

# Upgrades stop at the last ``pre`` revision of the installed release: ``post``
# revisions drop functions the previous release still calls, and we migrate at
# startup while old pods may still be serving. On the next procrastinate bump,
# move this to the new release's last ``pre``; that also applies this release's
# ``post`` revisions, safe by then because every pod runs this release.
PROCRASTINATE_REVISION = "procrastinate_0041"  # 03.12.00_03 pre (3.12's last pre)

# Where an ingest queue created before this adoption stands: a
# procrastinate 3.8-3.10 ``schema.sql`` plus 03.11.00_01, which our revision 011
# inlined.
PROCRASTINATE_ADOPTED_AT = "procrastinate_0038"


def _procrastinate_versions(url: str) -> str | None:
    """procrastinate's Alembic revisions, on Postgres with procrastinate installed.

    The ingest queue is Postgres-only, so SQLite never sees them.
    """
    if is_sqlite_url(url):
        return None
    try:
        return str(resources.files("procrastinate.contrib.alembic") / "versions")
    except ModuleNotFoundError:  # optional: the postgres/semantic extras bring it
        return None


def _coerce_url(database_url: str | Path | None) -> str:
    """Accept either a URL string, a Path (legacy SQLite path), or None.

    A bare ``Path`` is interpreted as a SQLite database file for backward
    compatibility with the prior path-based API.
    """
    if database_url is None:
        return get_database_url()
    if isinstance(database_url, Path):
        return f"sqlite+aiosqlite:///{database_url.resolve()}"
    return database_url


# Async-only drivers: strip the suffix so the sync engine falls back to the
# backend's default sync driver (``+aiosqlite`` -> ``sqlite`` / pysqlite).
_ASYNC_ONLY_DRIVERS = ("aiosqlite",)
# Drivers that already work synchronously — psycopg3 supports both sync and
# async, so the sync engine can use ``postgresql+psycopg://`` unchanged.
_SYNC_CAPABLE_DRIVERS = ("psycopg",)


def _to_sync_url(database_url: str) -> str:
    """Map an async driver URL to its sync equivalent for blocking inspection.

    SQLAlchemy's :func:`inspect` and :func:`create_engine` used below are
    synchronous APIs. ``+aiosqlite`` is async-only and gets stripped to the
    sync pysqlite driver; ``+psycopg`` is sync-capable and passes through
    unchanged (psycopg3 is a unified sync/async driver).

    Emits a one-shot warning when the URL carries some other unrecognized
    driver suffix — the sync engine creation downstream will still fail, but
    with a clearer hint than SQLAlchemy's generic "Can't load plugin" error.
    """
    out = database_url
    for driver in _ASYNC_ONLY_DRIVERS:
        out = out.replace(f"+{driver}", "")
    # Detect a leftover ``+<driver>`` token (we know the URL is
    # ``scheme[+driver]://...``, so a remaining ``+`` before ``://`` means a
    # driver we didn't strip). psycopg is sync-capable and expected; anything
    # else is unrecognized — log once and pass through.
    head = out.split("://", 1)[0]
    if "+" in head:
        driver = head.split("+", 1)[1]
        if driver not in _SYNC_CAPABLE_DRIVERS:
            logger.warning(
                "_to_sync_url: unrecognized driver %r in DATABASE_URL; "
                "passing through unchanged.",
                driver,
            )
    return out


def get_alembic_config(database_url: str | Path | None = None) -> Config:
    """
    Get Alembic configuration for programmatic use.

    Works in both development and installed (Docker) modes by using
    package location instead of alembic.ini file.

    Args:
        database_url: SQLAlchemy URL. If None, resolves via
            :func:`get_database_url` (DATABASE_URL env var, falling back
            to the ephemeral SQLite tempfile under ``TOKEN_STORAGE_DB``).
            For backward compatibility a ``Path`` is treated as a SQLite
            file path.

    Returns:
        Alembic Config object configured for the resolved URL.
    """
    if alembic_package.__file__ is None:
        raise RuntimeError("alembic package __file__ is None")
    script_location = Path(alembic_package.__file__).parent

    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("path_separator", "os")

    url = _coerce_url(database_url)
    config.set_main_option("sqlalchemy.url", url)

    logger.debug("Alembic script location: %s", script_location)
    logger.debug("Database URL: %s", mask_db_password(url))

    return config


def upgrade_database(
    database_url: str | Path | None = None, revision: str = "head"
) -> None:
    """Upgrade database to a specific revision (default: latest).

    Upgrading to ``head`` on Postgres with procrastinate installed then also
    migrates procrastinate's schema to :data:`PROCRASTINATE_REVISION`.
    """
    config = get_alembic_config(database_url)
    url = _coerce_url(database_url)
    procrastinate_versions = _procrastinate_versions(url)
    logger.info("Upgrading database to revision: %s", revision)
    with _ALEMBIC_COMMAND_LOCK:
        command.upgrade(config, revision)
        if revision == "head" and procrastinate_versions:
            # Same env.py, but only procrastinate's revisions, in their own table.
            config.set_main_option("version_locations", procrastinate_versions)
            config.set_main_option("version_table", PROCRASTINATE_VERSION_TABLE)
            if _untracked_ingest_queue(url):
                logger.info(
                    "Stamping existing ingest queue at %s", PROCRASTINATE_ADOPTED_AT
                )
                command.stamp(config, PROCRASTINATE_ADOPTED_AT)
            command.upgrade(config, PROCRASTINATE_REVISION)
    logger.info("Database upgrade completed successfully")


def _untracked_ingest_queue(url: str) -> bool:
    """True for an ingest queue whose schema procrastinate's revisions don't track yet."""
    engine = create_engine(_to_sync_url(url))
    try:
        with engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        "SELECT to_regclass('procrastinate_jobs') IS NOT NULL"
                        " AND to_regclass(:table) IS NULL"
                    ),
                    {"table": PROCRASTINATE_VERSION_TABLE},
                ).scalar()
            )
    finally:
        engine.dispose()


def downgrade_database(
    database_url: str | Path | None = None, revision: str = "-1"
) -> None:
    """Downgrade database to a specific revision (default: previous)."""
    config = get_alembic_config(database_url)
    logger.warning("Downgrading database to revision: %s", revision)
    with _ALEMBIC_COMMAND_LOCK:
        command.downgrade(config, revision)
    logger.info("Database downgrade completed successfully")


def get_current_revision(database_url: str | Path | None = None) -> str | None:
    """
    Get the current database revision by reading the ``alembic_version`` table.

    Returns ``None`` when the database does not exist or has no
    ``alembic_version`` table (i.e. has never been migrated).
    """
    url = _to_sync_url(_coerce_url(database_url))

    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///") :]
        if path and not Path(path).exists():
            logger.debug("Database does not exist: %s", path)
            return None

    try:
        engine = create_engine(url, future=True)
        try:
            inspector = inspect(engine)
            if not inspector.has_table("alembic_version"):
                return None
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).fetchone()
            return row[0] if row else None
        finally:
            engine.dispose()
    except Exception as e:
        logger.error("Failed to get current revision: %s", e)
        return None


def stamp_database(
    database_url: str | Path | None = None, revision: str = "head"
) -> None:
    """
    Stamp database with a specific revision without running migrations.

    Useful for marking pre-Alembic databases as already at a known revision.
    """
    config = get_alembic_config(database_url)
    logger.info("Stamping database with revision: %s", revision)
    with _ALEMBIC_COMMAND_LOCK:
        command.stamp(config, revision)
    logger.info("Database stamped successfully")


def show_migration_history(database_url: str | Path | None = None) -> None:
    """Display migration history."""
    config = get_alembic_config(database_url)
    with _ALEMBIC_COMMAND_LOCK:
        command.history(config, verbose=True)


def create_migration(message: str, autogenerate: bool = False) -> None:
    """
    Create a new migration script.

    Args:
        message: Description of the migration
        autogenerate: Whether to attempt auto-generation (requires SQLAlchemy models)

    Note:
        Since we don't use SQLAlchemy models, autogenerate will be disabled
        and migrations must be written manually using portable Alembic
        operations (``op.create_table``, ``op.add_column`` …) rather than
        raw SQL so they work on both SQLite and Postgres.
    """
    config = get_alembic_config()
    logger.info("Creating new migration: %s", message)

    if autogenerate:
        logger.warning(
            "Auto-generation is not supported (no SQLAlchemy models). "
            "Migration will be created with empty upgrade/downgrade functions."
        )

    with _ALEMBIC_COMMAND_LOCK:
        command.revision(config, message=message, autogenerate=False)
    logger.info("Migration created successfully. Edit the file to add SQL statements.")
