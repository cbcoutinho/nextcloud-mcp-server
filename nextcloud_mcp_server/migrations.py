"""Database migration utilities for nextcloud-mcp-server.

This module provides helper functions for managing Alembic database migrations
programmatically. It enables automatic migration on application startup and
provides CLI integration.
"""

import logging
from pathlib import Path

from alembic.config import Config

from alembic import command

logger = logging.getLogger(__name__)


def get_alembic_config(database_path: str | Path | None = None) -> Config:
    """
    Get Alembic configuration for programmatic use.

    Args:
        database_path: Path to SQLite database file. If None, uses default
                      from alembic.ini (/app/data/tokens.db)

    Returns:
        Alembic Config object configured for the specified database
    """
    # Get path to alembic.ini (in project root)
    project_root = Path(__file__).parent.parent
    alembic_ini_path = project_root / "alembic.ini"

    if not alembic_ini_path.exists():
        raise FileNotFoundError(
            f"alembic.ini not found at {alembic_ini_path}. "
            "Ensure Alembic is properly initialized."
        )

    # Create Alembic config
    config = Config(str(alembic_ini_path))

    # Override database URL if provided
    if database_path:
        db_path = Path(database_path).resolve()
        # Use sqlite+aiosqlite:// for async support
        url = f"sqlite+aiosqlite:///{db_path}"
        config.set_main_option("sqlalchemy.url", url)
        logger.debug(f"Alembic configured with database: {db_path}")

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
