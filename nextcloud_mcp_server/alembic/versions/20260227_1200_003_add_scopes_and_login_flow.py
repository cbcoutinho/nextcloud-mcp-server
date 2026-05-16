"""Add scopes and login flow sessions for Login Flow v2

This migration adds support for:
1. Scoped app passwords (scopes column + username column on app_passwords)
2. Login Flow v2 session tracking (login_flow_sessions table)

Nullable scopes preserves backward compat: NULL = legacy app password = all scopes allowed.

Revision ID: 003
Revises: 002
Create Date: 2026-02-27 12:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add scopes/username to app_passwords and create login_flow_sessions."""

    # Nullable scope columns on the existing app_passwords table.
    op.add_column("app_passwords", sa.Column("scopes", sa.Text))
    op.add_column("app_passwords", sa.Column("username", sa.Text))

    op.create_table(
        "login_flow_sessions",
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("encrypted_poll_token", sa.LargeBinary, nullable=False),
        sa.Column("poll_endpoint", sa.Text, nullable=False),
        sa.Column("requested_scopes", sa.Text),
        # BigInteger to keep unix epochs in range on Postgres (see 001).
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=False),
    )
    op.create_index(
        "idx_login_flow_sessions_expires",
        "login_flow_sessions",
        ["expires_at"],
    )


def downgrade() -> None:
    """Drop login_flow_sessions and remove added columns.

    ``batch_alter_table`` handles SQLite's pre-3.35 lack of ``DROP COLUMN``
    by recreating the table; on Postgres it issues a native ``DROP COLUMN``.
    """

    op.drop_index("idx_login_flow_sessions_expires", table_name="login_flow_sessions")
    op.drop_table("login_flow_sessions")

    with op.batch_alter_table("app_passwords") as batch_op:
        batch_op.drop_column("username")
        batch_op.drop_column("scopes")
