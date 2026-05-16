"""Add browser_sessions table for random-id browser cookie auth.

Replaces the prior `mcp_session=<user_id>` cookie pattern (issue #626
finding 2) with a server-side mapping from a cryptographically random
session id to the authenticated user_id. The cookie value is now opaque
and revocable.

Revision ID: 005
Revises: 004
Create Date: 2026-05-02 15:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("session_id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, nullable=False),
        # BigInteger to keep unix epochs in range on Postgres (see 001).
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=False),
    )
    op.create_index("idx_browser_sessions_user", "browser_sessions", ["user_id"])
    op.create_index("idx_browser_sessions_expires", "browser_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("idx_browser_sessions_expires", table_name="browser_sessions")
    op.drop_index("idx_browser_sessions_user", table_name="browser_sessions")
    op.drop_table("browser_sessions")
