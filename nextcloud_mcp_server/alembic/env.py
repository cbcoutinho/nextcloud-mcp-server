"""Alembic environment configuration for nextcloud-mcp-server.

This module configures how Alembic runs database migrations for the
token storage database. It supports both online and offline migration modes.

Uses anyio for async operations, consistent with the project's async patterns.
"""

import logging
from pathlib import Path

import anyio
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Configure logging
logger = logging.getLogger("alembic.env")

# This is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Update script location to point to package location
# This allows alembic to find migrations when installed in site-packages
script_location = Path(__file__).parent
config.set_main_option("script_location", str(script_location))

# We don't use SQLAlchemy models, so target_metadata is None
# Migrations will be written manually using op.execute() for raw SQL
target_metadata = None


def get_database_url() -> str:
    """
    Get the database URL from Alembic config or environment.

    The URL can be set in alembic.ini or passed via -x database_url=...
    when running Alembic commands.

    Returns:
        Database URL (SQLite URL format)
    """
    # Check if URL is passed via -x database_url=...
    url = context.get_x_argument(as_dictionary=True).get("database_url")

    if not url:
        # Fall back to alembic.ini configuration
        url = config.get_main_option("sqlalchemy.url")

    if not url:
        # Default to /app/data/tokens.db for Docker deployments
        db_path = Path("/app/data/tokens.db")
        url = f"sqlite+aiosqlite:///{db_path}"
        logger.warning(
            f"No database URL configured, using default: {url}. "
            "Set sqlalchemy.url in alembic.ini or pass -x database_url=..."
        )

    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well. By skipping the
    Engine creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    This mode is useful for generating SQL scripts without database access.
    """
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Execute migrations within a database connection."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async support.

    In this scenario we create an async Engine and associate
    a connection with the context.
    """
    # Get database URL and update config
    url = get_database_url()
    config.set_main_option("sqlalchemy.url", url)

    # Create async engine
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # Don't pool connections for migrations
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    This function is called from storage.py's initialize() method via
    anyio.to_thread.run_sync(), so it always runs in a worker thread
    with its own event loop. We can safely use anyio.run() here.
    """
    anyio.run(run_async_migrations)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
