"""Add app_passwords table for multi-user BasicAuth mode

This migration adds support for storing app passwords that are provisioned
via Astrolabe's personal settings. This enables background sync in
multi-user BasicAuth mode without requiring OAuth.

Revision ID: 002
Revises: 001
Create Date: 2026-01-13 12:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add app_passwords table for multi-user BasicAuth mode."""

    op.create_table(
        "app_passwords",
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("encrypted_password", sa.LargeBinary, nullable=False),
        # BigInteger to keep unix epochs in range on Postgres (see 001).
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=False),
    )
    op.create_index("idx_app_passwords_updated", "app_passwords", ["updated_at"])


def downgrade() -> None:
    """Drop app_passwords table."""

    op.drop_index("idx_app_passwords_updated", table_name="app_passwords")
    op.drop_table("app_passwords")
