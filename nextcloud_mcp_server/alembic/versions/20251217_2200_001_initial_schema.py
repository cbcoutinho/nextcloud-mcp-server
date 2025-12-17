"""Initial schema for token storage database

This migration creates the initial database schema including:
- refresh_tokens: OAuth refresh tokens and user profiles
- audit_logs: Audit trail for security events
- oauth_clients: OAuth client credentials (DCR)
- oauth_sessions: OAuth flow session state (ADR-004 Progressive Consent)
- registered_webhooks: Webhook registration tracking (both OAuth and BasicAuth)
- schema_version: Legacy schema version tracking (deprecated, use alembic_version)

Revision ID: 001
Revises:
Create Date: 2025-12-17 22:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create initial database schema."""

    # Refresh tokens table (OAuth mode only, for background jobs)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            user_id TEXT PRIMARY KEY,
            encrypted_token BLOB NOT NULL,
            expires_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            -- ADR-004 Progressive Consent fields
            flow_type TEXT DEFAULT 'hybrid',
            token_audience TEXT DEFAULT 'nextcloud',
            provisioned_at INTEGER,
            provisioning_client_id TEXT,
            scopes TEXT,
            -- Browser session profile cache
            user_profile TEXT,
            profile_cached_at INTEGER
        )
        """
    )

    # Audit logs table (both OAuth and BasicAuth modes)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            event TEXT NOT NULL,
            user_id TEXT NOT NULL,
            resource_type TEXT,
            resource_id TEXT,
            auth_method TEXT,
            hostname TEXT
        )
        """
    )

    # Index on audit logs for efficient queries
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_user_timestamp
        ON audit_logs(user_id, timestamp)
        """
    )

    # OAuth client credentials storage (OAuth mode only)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_clients (
            id INTEGER PRIMARY KEY,
            client_id TEXT UNIQUE NOT NULL,
            encrypted_client_secret BLOB NOT NULL,
            client_id_issued_at INTEGER NOT NULL,
            client_secret_expires_at INTEGER NOT NULL,
            redirect_uris TEXT NOT NULL,
            encrypted_registration_access_token BLOB,
            registration_client_uri TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    # OAuth flow sessions (ADR-004 Progressive Consent)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_sessions (
            session_id TEXT PRIMARY KEY,
            client_id TEXT,
            client_redirect_uri TEXT NOT NULL,
            state TEXT,
            code_challenge TEXT,
            code_challenge_method TEXT,
            mcp_authorization_code TEXT UNIQUE,
            idp_access_token TEXT,
            idp_refresh_token TEXT,
            user_id TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            -- ADR-004 Progressive Consent fields
            flow_type TEXT DEFAULT 'hybrid',
            requested_scopes TEXT,
            granted_scopes TEXT,
            is_provisioning BOOLEAN DEFAULT FALSE
        )
        """
    )

    # Index for MCP authorization code lookups
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_oauth_sessions_mcp_code
        ON oauth_sessions(mcp_authorization_code)
        """
    )

    # Legacy schema version tracking table
    # NOTE: This is deprecated in favor of Alembic's alembic_version table
    # Kept for backward compatibility with pre-Alembic databases
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        )
        """
    )

    # Registered webhooks tracking (both BasicAuth and OAuth modes)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS registered_webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            webhook_id INTEGER NOT NULL UNIQUE,
            preset_id TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )

    # Indexes for efficient webhook queries
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_webhooks_preset
        ON registered_webhooks(preset_id)
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_webhooks_created
        ON registered_webhooks(created_at)
        """
    )


def downgrade() -> None:
    """Drop all tables and indexes.

    WARNING: This will destroy all data in the database!
    Use with extreme caution.
    """

    # Drop indexes first
    op.execute("DROP INDEX IF EXISTS idx_webhooks_created")
    op.execute("DROP INDEX IF EXISTS idx_webhooks_preset")
    op.execute("DROP INDEX IF EXISTS idx_oauth_sessions_mcp_code")
    op.execute("DROP INDEX IF EXISTS idx_audit_user_timestamp")

    # Drop tables
    op.execute("DROP TABLE IF EXISTS registered_webhooks")
    op.execute("DROP TABLE IF EXISTS schema_version")
    op.execute("DROP TABLE IF EXISTS oauth_sessions")
    op.execute("DROP TABLE IF EXISTS oauth_clients")
    op.execute("DROP TABLE IF EXISTS audit_logs")
    op.execute("DROP TABLE IF EXISTS refresh_tokens")
