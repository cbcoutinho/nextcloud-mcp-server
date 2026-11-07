"""Add refresh_token_clients table binding AS-proxy refresh tokens to clients.

The AS proxy (ADR-023) refreshes against the IdP with the MCP server's own
client credentials, so the IdP cannot tell which MCP client a refresh token
was issued to — and the token endpoint never checked either. A refresh token
issued to one client was accepted from any other ``client_id``, or from none
(RFC 6749 §6).

Each row maps a SHA-256 of a refresh token handed out by the proxy to the
``client_id`` it was handed to. The token itself is never stored: the hash is
enough to look up the binding, and useless to anyone who reads the table.

Revision ID: 011
Revises: 010
Create Date: 2026-10-08 12:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_token_clients",
        sa.Column("token_hash", sa.Text, primary_key=True),
        sa.Column("client_id", sa.Text, nullable=False),
        # BigInteger to keep unix epochs in range on Postgres (see 001).
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=False),
    )
    op.create_index(
        "idx_refresh_token_clients_expires", "refresh_token_clients", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "idx_refresh_token_clients_expires", table_name="refresh_token_clients"
    )
    op.drop_table("refresh_token_clients")
