"""Add app_passwords table for multi-user BasicAuth mode

This migration adds support for storing app passwords that are provisioned
via Astrolabe's personal settings. This enables background sync in
multi-user BasicAuth mode without requiring OAuth.

Revision ID: 002
Revises: 001
Create Date: 2026-01-13 12:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add app_passwords table for multi-user BasicAuth mode."""

    # App passwords table for multi-user BasicAuth background sync
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_passwords (
            user_id TEXT PRIMARY KEY,
            encrypted_password BLOB NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    # Index for efficient user lookups
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_app_passwords_updated
        ON app_passwords(updated_at)
        """
    )


def downgrade() -> None:
    """Drop app_passwords table."""

    op.execute("DROP INDEX IF EXISTS idx_app_passwords_updated")
    op.execute("DROP TABLE IF EXISTS app_passwords")
