"""Add nonce column to oauth_sessions for OIDC ID-token binding.

PR #758 finding 2: the browser OAuth flow generated PKCE + state but no
``nonce``. Without a nonce, an attacker who obtains a valid ID token for
another user (e.g. from a parallel auth request) could replay it inside
this flow because the token isn't cryptographically tied to the
authorization request. The nonce is generated in ``oauth_login``,
forwarded to the IdP in the auth URL, and verified on the way back.

Revision ID: 006
Revises: 005
Create Date: 2026-05-02 16:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``batch_alter_table`` emits a native ``ALTER TABLE ... ADD COLUMN``
    # on Postgres and works around SQLite's pre-3.35 limitations by
    # recreating the table when needed. Matches the portable-DDL style of
    # the rewritten migrations 001-005 (PR #798 review nit).
    with op.batch_alter_table("oauth_sessions") as batch_op:
        batch_op.add_column(sa.Column("nonce", sa.Text))


def downgrade() -> None:
    with op.batch_alter_table("oauth_sessions") as batch_op:
        batch_op.drop_column("nonce")
