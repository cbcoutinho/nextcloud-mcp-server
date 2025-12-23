"""Database migration utilities for nextcloud-mcp-server.

This module provides helper functions for managing Alembic database migrations
programmatically. It enables automatic migration on application startup and
provides CLI integration.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)


def get_alembic_config(database_path: str | Path | None = None) -> Config:
    """
    Get Alembic configuration for programmatic use.

    Works in both development and installed (Docker) modes by using
    package location instead of alembic.ini file.

    Args:
        database_path: Path to SQLite database file. If None, uses default
                      (/app/data/tokens.db for Docker)

    Returns:
        Alembic Config object configured for the specified database
    """
    from nextcloud_mcp_server import alembic as alembic_package

    # Use package location (works in both editable and installed modes)
    if alembic_package.__file__ is None:
        raise RuntimeError("alembic package __file__ is None")
    script_location = Path(alembic_package.__file__).parent

    # Create config programmatically (no alembic.ini needed at runtime)
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("path_separator", "os")  # Suppress deprecation warning

    # Set database URL
    if database_path:
        db_path = Path(database_path).resolve()
    else:
        db_path = Path("/app/data/tokens.db")  # Default for Docker

    url = f"sqlite+aiosqlite:///{db_path}"
    config.set_main_option("sqlalchemy.url", url)

    logger.debug(f"Alembic script location: {script_location}")
    logger.debug(f"Database: {db_path}")

    return config


def upgrade_database(
    database_path: str | Path | None = None, revision: str = "head"
) -> None:
    """
    Upgrade database to a specific revision.

    Args:
        database_path: Path to SQLite database file
        revision: Target revision (default: "head" for latest)
    """
    config = get_alembic_config(database_path)
    logger.info(f"Upgrading database to revision: {revision}")
    command.upgrade(config, revision)
    logger.info("Database upgrade completed successfully")


def downgrade_database(
    database_path: str | Path | None = None, revision: str = "-1"
) -> None:
    """
    Downgrade database to a specific revision.

    Args:
        database_path: Path to SQLite database file
        revision: Target revision (default: "-1" for previous version)
    """
    config = get_alembic_config(database_path)
    logger.warning(f"Downgrading database to revision: {revision}")
    command.downgrade(config, revision)
    logger.info("Database downgrade completed successfully")


def get_current_revision(database_path: str | Path | None = None) -> str | None:
    """
    Get the current database revision by directly querying the alembic_version table.

    Args:
        database_path: Path to SQLite database file

    Returns:
        Current revision ID or None if not versioned
    """
    import sqlite3

    if database_path is None:
        database_path = "/app/data/tokens.db"

    db_path = Path(database_path).resolve()

    if not db_path.exists():
        logger.debug(f"Database does not exist: {db_path}")
        return None

    try:
        # Query alembic_version table directly
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Check if alembic_version table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        )
        has_table = cursor.fetchone() is not None

        if not has_table:
            conn.close()
            return None

        # Get current version
        cursor.execute("SELECT version_num FROM alembic_version")
        row = cursor.fetchone()
        conn.close()

        return row[0] if row else None

    except Exception as e:
        logger.error(f"Failed to get current revision: {e}")
        return None


def stamp_database(
    database_path: str | Path | None = None, revision: str = "head"
) -> None:
    """
    Stamp database with a specific revision without running migrations.

    This is useful for marking existing databases that were created before
    Alembic was introduced. It tells Alembic "this database is at revision X"
    without actually running the migration.

    Args:
        database_path: Path to SQLite database file
        revision: Revision to stamp (default: "head" for latest)
    """
    config = get_alembic_config(database_path)
    logger.info(f"Stamping database with revision: {revision}")
    command.stamp(config, revision)
    logger.info("Database stamped successfully")


def show_migration_history(database_path: str | Path | None = None) -> None:
    """
    Display migration history.

    Args:
        database_path: Path to SQLite database file
    """
    config = get_alembic_config(database_path)
    command.history(config, verbose=True)


def create_migration(message: str, autogenerate: bool = False) -> None:
    """
    Create a new migration script.

    Args:
        message: Description of the migration
        autogenerate: Whether to attempt auto-generation (requires SQLAlchemy models)

    Note:
        Since we don't use SQLAlchemy models, autogenerate will be disabled
        and migrations must be written manually.
    """
    config = get_alembic_config()
    logger.info(f"Creating new migration: {message}")

    if autogenerate:
        logger.warning(
            "Auto-generation is not supported (no SQLAlchemy models). "
            "Migration will be created with empty upgrade/downgrade functions."
        )

    command.revision(config, message=message, autogenerate=False)
    logger.info("Migration created successfully. Edit the file to add SQL statements.")
