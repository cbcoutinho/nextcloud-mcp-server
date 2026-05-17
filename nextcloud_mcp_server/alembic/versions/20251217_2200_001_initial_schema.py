"""Initial schema for token storage database

This migration creates the initial database schema including:
- refresh_tokens: OAuth refresh tokens and user profiles
- audit_logs: Audit trail for security events
- oauth_clients: OAuth client credentials (DCR)
- oauth_sessions: OAuth flow session state (ADR-004 Progressive Consent)
- registered_webhooks: Webhook registration tracking (both OAuth and BasicAuth)
- schema_version: Legacy schema version tracking (deprecated, use alembic_version)

Uses Alembic's portable schema-DDL helpers (``op.create_table`` /
``op.create_index``) with SQLAlchemy types so the DDL is emitted correctly
for both SQLite (BLOB / INTEGER PRIMARY KEY AUTOINCREMENT) and Postgres
(BYTEA / SERIAL). See ADR-026.

Revision ID: 001
Revises:
Create Date: 2025-12-17 22:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create initial database schema.

    All ``*_at`` / expiration / timestamp columns use :class:`sa.BigInteger`
    so Postgres allocates BIGINT (8-byte) and unix epoch values don't
    overflow the 32-bit int32 INTEGER range on long-lived sessions. SQLite
    treats BIGINT and INTEGER identically (dynamic typing), so this is
    backwards compatible.
    """

    op.create_table(
        "refresh_tokens",
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("encrypted_token", sa.LargeBinary, nullable=False),
        sa.Column("expires_at", sa.BigInteger),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=False),
        # ADR-004 Progressive Consent fields
        sa.Column("flow_type", sa.Text, server_default="hybrid"),
        sa.Column("token_audience", sa.Text, server_default="nextcloud"),
        sa.Column("provisioned_at", sa.BigInteger),
        sa.Column("provisioning_client_id", sa.Text),
        sa.Column("scopes", sa.Text),
        # Browser session profile cache
        sa.Column("user_profile", sa.Text),
        sa.Column("profile_cached_at", sa.BigInteger),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.BigInteger, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("resource_type", sa.Text),
        sa.Column("resource_id", sa.Text),
        sa.Column("auth_method", sa.Text),
        sa.Column("hostname", sa.Text),
    )
    op.create_index("idx_audit_user_timestamp", "audit_logs", ["user_id", "timestamp"])

    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("client_id", sa.Text, nullable=False, unique=True),
        sa.Column("encrypted_client_secret", sa.LargeBinary, nullable=False),
        sa.Column("client_id_issued_at", sa.BigInteger, nullable=False),
        sa.Column("client_secret_expires_at", sa.BigInteger, nullable=False),
        sa.Column("redirect_uris", sa.Text, nullable=False),
        sa.Column("encrypted_registration_access_token", sa.LargeBinary),
        sa.Column("registration_client_uri", sa.Text),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=False),
    )

    op.create_table(
        "oauth_sessions",
        sa.Column("session_id", sa.Text, primary_key=True),
        sa.Column("client_id", sa.Text),
        sa.Column("client_redirect_uri", sa.Text, nullable=False),
        sa.Column("state", sa.Text),
        sa.Column("code_challenge", sa.Text),
        sa.Column("code_challenge_method", sa.Text),
        sa.Column("mcp_authorization_code", sa.Text, unique=True),
        sa.Column("idp_access_token", sa.Text),
        sa.Column("idp_refresh_token", sa.Text),
        sa.Column("user_id", sa.Text),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=False),
        # ADR-004 Progressive Consent fields
        sa.Column("flow_type", sa.Text, server_default="hybrid"),
        sa.Column("requested_scopes", sa.Text),
        sa.Column("granted_scopes", sa.Text),
        sa.Column("is_provisioning", sa.Boolean, server_default=sa.false()),
    )
    op.create_index(
        "idx_oauth_sessions_mcp_code",
        "oauth_sessions",
        ["mcp_authorization_code"],
    )

    # Legacy schema-version table; superseded by alembic_version. Only
    # created on SQLite because it exists *purely* to match the
    # fingerprint of pre-Alembic SQLite databases that get stamped into
    # the migration chain (see ``RefreshTokenStorage.initialize()``).
    # Fresh Postgres installs have no pre-Alembic history and don't
    # need it. PR #798 round-3 review (#4).
    if op.get_bind().dialect.name == "sqlite":
        op.create_table(
            "schema_version",
            sa.Column("version", sa.Integer, primary_key=True, autoincrement=False),
            sa.Column("applied_at", sa.Float, nullable=False),
        )

    op.create_table(
        "registered_webhooks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("webhook_id", sa.Integer, nullable=False, unique=True),
        sa.Column("preset_id", sa.Text, nullable=False),
        # BigInteger for consistency with every other *_at column (PR #798
        # review): subsecond precision wasn't load-bearing for webhook
        # bookkeeping. ``store_webhook()`` casts to ``int(time.time())``.
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )
    op.create_index("idx_webhooks_preset", "registered_webhooks", ["preset_id"])
    op.create_index("idx_webhooks_created", "registered_webhooks", ["created_at"])


def downgrade() -> None:
    """Drop all tables and indexes.

    WARNING: This will destroy all data in the database!
    Use with extreme caution.
    """

    op.drop_index("idx_webhooks_created", table_name="registered_webhooks")
    op.drop_index("idx_webhooks_preset", table_name="registered_webhooks")
    op.drop_table("registered_webhooks")
    # ``schema_version`` is only created on SQLite (see ``upgrade()``).
    if op.get_bind().dialect.name == "sqlite":
        op.drop_table("schema_version")
    op.drop_index("idx_oauth_sessions_mcp_code", table_name="oauth_sessions")
    op.drop_table("oauth_sessions")
    op.drop_table("oauth_clients")
    op.drop_index("idx_audit_user_timestamp", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("refresh_tokens")
