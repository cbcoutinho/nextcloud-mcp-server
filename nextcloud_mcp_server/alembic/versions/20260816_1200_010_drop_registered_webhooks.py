"""Drop the registered_webhooks table.

The MCP server no longer registers webhooks with Nextcloud: Astrolabe subscribes
to Nextcloud's change events itself and POSTs them to ``/webhooks/nextcloud``, so
there is no registration id to track. The registration API, the ``/app/webhooks``
pane and the preset catalogue that wrote this table are gone; the table is dead
bookkeeping.

Dropping it loses nothing recoverable — the rows only mapped Nextcloud webhook
ids to preset ids for an integration that no longer exists. The downgrade
recreates the (empty) table so the schema round-trips.

Revision ID: 010
Revises: 009
Create Date: 2026-08-16 12:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Indexes go with the table on both dialects; drop them explicitly so the
    # migration is symmetric with 001, which created them explicitly.
    op.drop_index("idx_webhooks_created", table_name="registered_webhooks")
    op.drop_index("idx_webhooks_preset", table_name="registered_webhooks")
    op.drop_table("registered_webhooks")


def downgrade() -> None:
    op.create_table(
        "registered_webhooks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("webhook_id", sa.Integer, nullable=False, unique=True),
        sa.Column("preset_id", sa.Text, nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )
    op.create_index("idx_webhooks_preset", "registered_webhooks", ["preset_id"])
    op.create_index("idx_webhooks_created", "registered_webhooks", ["created_at"])
