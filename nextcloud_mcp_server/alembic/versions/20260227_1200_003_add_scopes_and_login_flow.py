"""Add scopes and login flow sessions for Login Flow v2

This migration adds support for:
1. Scoped app passwords (scopes column + username column on app_passwords)
2. Login Flow v2 session tracking (login_flow_sessions table)

Nullable scopes preserves backward compat: NULL = legacy app password = all scopes allowed.

Revision ID: 003
Revises: 002
Create Date: 2026-02-27 12:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add scopes/username to app_passwords and create login_flow_sessions."""

    # Add scopes column (nullable JSON array, NULL = all scopes allowed)
    op.execute(
        """
        ALTER TABLE app_passwords ADD COLUMN scopes TEXT
        """
    )

    # Add username column (Nextcloud loginName from Login Flow v2)
    op.execute(
        """
        ALTER TABLE app_passwords ADD COLUMN username TEXT
        """
    )

    # Login Flow v2 session tracking
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS login_flow_sessions (
            user_id TEXT PRIMARY KEY,
            encrypted_poll_token BLOB NOT NULL,
            poll_endpoint TEXT NOT NULL,
            requested_scopes TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
        """
    )

    # Index for efficient cleanup of expired sessions
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_login_flow_sessions_expires
        ON login_flow_sessions(expires_at)
        """
    )


def downgrade() -> None:
    """Drop login_flow_sessions and remove added columns."""

    op.execute("DROP INDEX IF EXISTS idx_login_flow_sessions_expires")
    op.execute("DROP TABLE IF EXISTS login_flow_sessions")

    # SQLite doesn't support DROP COLUMN before 3.35.0
    # Recreate app_passwords without the new columns
    op.execute(
        """
        CREATE TABLE app_passwords_backup (
            user_id TEXT PRIMARY KEY,
            encrypted_password BLOB NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO app_passwords_backup (user_id, encrypted_password, created_at, updated_at)
        SELECT user_id, encrypted_password, created_at, updated_at FROM app_passwords
        """
    )
    op.execute("DROP TABLE app_passwords")
    op.execute("ALTER TABLE app_passwords_backup RENAME TO app_passwords")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_app_passwords_updated
        ON app_passwords(updated_at)
        """
    )
