"""
Persistent Storage for MCP Server State

This module provides SQLite-based storage for multiple concerns across both
BasicAuth and OAuth authentication modes:

1. **Refresh Tokens** (OAuth mode only, for background jobs)
   - Securely stores encrypted refresh tokens for offline access
   - Used ONLY by background jobs to obtain access tokens
   - NEVER used within MCP client sessions or browser sessions

2. **User Profile Cache** (OAuth mode only, for browser UI display)
   - Caches IdP user profile data for browser-based admin UI
   - Queried ONCE at login, displayed from cache thereafter
   - NOT used for authorization decisions or background jobs

3. **Webhook Registration Tracking** (both modes, for webhook management)
   - Tracks registered webhook IDs mapped to presets
   - Enables persistent webhook state across restarts
   - Avoids redundant Nextcloud API calls for webhook status

IMPORTANT: The database is initialized in both BasicAuth and OAuth modes.
Token storage requires TOKEN_ENCRYPTION_KEY, but webhook tracking does not.

Sensitive data (tokens, secrets) is encrypted at rest using Fernet symmetric encryption.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite
from cryptography.fernet import Fernet

from nextcloud_mcp_server.observability.metrics import record_db_operation

logger = logging.getLogger(__name__)


class RefreshTokenStorage:
    """Persistent storage for MCP server state (tokens, webhooks, and future features).

    This class manages multiple concerns across both BasicAuth and OAuth modes:

    **OAuth-specific concerns**:
    - Refresh tokens: Encrypted storage for background job access (requires encryption key)
    - User profiles: Plain JSON cache for browser UI display
    - OAuth client credentials: Encrypted client secrets from DCR
    - OAuth sessions: Temporary session state for progressive consent flow

    **Both modes**:
    - Webhook registration: Track registered webhooks mapped to presets
    - Schema versioning: Handle database migrations automatically

    Token-related operations require TOKEN_ENCRYPTION_KEY, but webhook operations do not.
    """

    def __init__(self, db_path: str, encryption_key: bytes | None = None):
        """
        Initialize persistent storage.

        Args:
            db_path: Path to SQLite database file
            encryption_key: Optional Fernet encryption key (32 bytes, base64-encoded).
                          Required for token storage operations, not required for webhook tracking.
        """
        self.db_path = db_path
        self.cipher = Fernet(encryption_key) if encryption_key else None
        self._initialized = False

    @classmethod
    def from_env(cls) -> "RefreshTokenStorage":
        """
        Create storage instance from environment variables.

        Environment variables:
            TOKEN_STORAGE_DB: Path to database file (default: /app/data/tokens.db)
            TOKEN_ENCRYPTION_KEY: Optional base64-encoded Fernet key (required for token storage)

        Returns:
            RefreshTokenStorage instance

        Note:
            If TOKEN_ENCRYPTION_KEY is not set, token storage operations will fail,
            but webhook tracking will still work.
        """
        db_path = os.getenv("TOKEN_STORAGE_DB", "/app/data/tokens.db")
        encryption_key_b64 = os.getenv("TOKEN_ENCRYPTION_KEY")

        encryption_key = None
        if encryption_key_b64:
            # Fernet expects a base64url-encoded key as bytes, not decoded bytes
            # The key from Fernet.generate_key() is already base64url-encoded
            try:
                # Convert string to bytes if needed
                if isinstance(encryption_key_b64, str):
                    encryption_key = encryption_key_b64.encode()
                else:
                    encryption_key = encryption_key_b64

                # Validate the key by trying to create a Fernet instance
                Fernet(encryption_key)
            except Exception as e:
                raise ValueError(
                    f"Invalid TOKEN_ENCRYPTION_KEY: {e}. "
                    "Must be a valid Fernet key (base64url-encoded 32 bytes)."
                ) from e
        else:
            logger.info(
                "TOKEN_ENCRYPTION_KEY not set - token storage operations will be unavailable, "
                "but webhook tracking will still work"
            )

        return cls(db_path=db_path, encryption_key=encryption_key)

    async def initialize(self) -> None:
        """Initialize database schema"""
        if self._initialized:
            return

        # Ensure directory exists
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        # Set restrictive permissions on database file
        if Path(self.db_path).exists():
            os.chmod(self.db_path, 0o600)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    user_id TEXT PRIMARY KEY,
                    encrypted_token BLOB NOT NULL,
                    expires_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    -- ADR-004 Progressive Consent fields
                    flow_type TEXT DEFAULT 'hybrid',  -- 'hybrid', 'flow1', 'flow2'
                    token_audience TEXT DEFAULT 'nextcloud',  -- 'mcp-server' or 'nextcloud'
                    provisioned_at INTEGER,  -- When Flow 2 was completed
                    provisioning_client_id TEXT,  -- Which MCP client initiated Flow 1
                    scopes TEXT,  -- JSON array of granted scopes
                    -- Browser session profile cache
                    user_profile TEXT,  -- JSON cache of IdP user profile (for browser UI only)
                    profile_cached_at INTEGER  -- When profile was last cached
                )
                """
            )

            await db.execute(
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

            # Create index on audit logs for efficient queries
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_user_timestamp "
                "ON audit_logs(user_id, timestamp)"
            )

            # OAuth client credentials storage
            await db.execute(
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
            await db.execute(
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
                    flow_type TEXT DEFAULT 'hybrid',  -- 'hybrid', 'flow1', 'flow2'
                    requested_scopes TEXT,  -- JSON array of requested scopes
                    granted_scopes TEXT,  -- JSON array of granted scopes
                    is_provisioning BOOLEAN DEFAULT FALSE  -- True if this is a Flow 2 provisioning session
                )
                """
            )

            # Create index for MCP authorization code lookups
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_oauth_sessions_mcp_code "
                "ON oauth_sessions(mcp_authorization_code)"
            )

            # Schema version tracking
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL
                )
                """
            )

            # Registered webhooks tracking (both BasicAuth and OAuth modes)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS registered_webhooks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    webhook_id INTEGER NOT NULL UNIQUE,
                    preset_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

            # Create indexes for efficient webhook queries
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_webhooks_preset "
                "ON registered_webhooks(preset_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_webhooks_created "
                "ON registered_webhooks(created_at)"
            )

            await db.commit()

        # Set restrictive permissions after creation
        os.chmod(self.db_path, 0o600)

        self._initialized = True
        logger.info(f"Initialized refresh token storage at {self.db_path}")

    async def store_refresh_token(
        self,
        user_id: str,
        refresh_token: str,
        expires_at: Optional[int] = None,
        flow_type: str = "hybrid",
        token_audience: str = "nextcloud",
        provisioning_client_id: Optional[str] = None,
        scopes: Optional[list[str]] = None,
    ) -> None:
        """
        Store encrypted refresh token for user.

        Args:
            user_id: User identifier (from OIDC 'sub' claim)
            refresh_token: Refresh token to store
            expires_at: Token expiration timestamp (Unix epoch), if known
            flow_type: Type of flow ('hybrid', 'flow1', 'flow2')
            token_audience: Token audience ('mcp-server' or 'nextcloud')
            provisioning_client_id: Client ID that initiated Flow 1
            scopes: List of granted scopes

        """
        if not self._initialized:
            await self.initialize()

        encrypted_token = self.cipher.encrypt(refresh_token.encode())
        now = int(time.time())
        scopes_json = json.dumps(scopes) if scopes else None

        # For Flow 2, set provisioned_at timestamp
        provisioned_at = now if flow_type == "flow2" else None

        start_time = time.time()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO refresh_tokens
                    (user_id, encrypted_token, expires_at, created_at, updated_at,
                     flow_type, token_audience, provisioned_at, provisioning_client_id, scopes)
                    VALUES (?, ?, ?, COALESCE((SELECT created_at FROM refresh_tokens WHERE user_id = ?), ?), ?,
                            ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        encrypted_token,
                        expires_at,
                        user_id,
                        now,
                        now,
                        flow_type,
                        token_audience,
                        provisioned_at,
                        provisioning_client_id,
                        scopes_json,
                    ),
                )
                await db.commit()
            duration = time.time() - start_time
            record_db_operation("sqlite", "insert", duration, "success")

            logger.info(
                f"Stored refresh token for user {user_id}"
                + (f" (expires at {expires_at})" if expires_at else "")
            )
        except Exception:
            duration = time.time() - start_time
            record_db_operation("sqlite", "insert", duration, "error")
            raise

        # Audit log
        await self._audit_log(
            event="store_refresh_token",
            user_id=user_id,
            auth_method="offline_access",
        )

    async def store_user_profile(
        self, user_id: str, profile_data: dict[str, Any]
    ) -> None:
        """
        Store user profile data (cached from IdP userinfo endpoint).

        This profile is cached ONLY for browser UI display purposes, not for
        authorization decisions. Background jobs should NOT rely on this data.

        Args:
            user_id: User identifier (must match refresh_tokens.user_id)
            profile_data: User profile dict from IdP userinfo endpoint
        """
        if not self._initialized:
            await self.initialize()

        profile_json = json.dumps(profile_data)
        now = int(time.time())

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE refresh_tokens
                SET user_profile = ?, profile_cached_at = ?
                WHERE user_id = ?
                """,
                (profile_json, now, user_id),
            )
            await db.commit()

        logger.debug(f"Cached user profile for {user_id}")

    async def get_user_profile(self, user_id: str) -> Optional[dict[str, Any]]:
        """
        Retrieve cached user profile data.

        This returns cached profile data from the initial OAuth login,
        NOT fresh data from the IdP. Use this for browser UI display only.

        Args:
            user_id: User identifier

        Returns:
            User profile dict or None if not cached
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT user_profile, profile_cached_at
                FROM refresh_tokens
                WHERE user_id = ?
                """,
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row or not row[0]:
            return None

        profile_json, cached_at = row
        profile_data = json.loads(profile_json)

        # Optionally add cache metadata
        profile_data["_cached_at"] = cached_at

        return profile_data

    async def get_refresh_token(self, user_id: str) -> Optional[dict]:
        """
        Retrieve and decrypt refresh token for user.

        Args:
            user_id: User identifier

        Returns:
            Dictionary with token data including ADR-004 fields:
            {
                "refresh_token": str,
                "expires_at": int | None,
                "flow_type": str,
                "token_audience": str,
                "provisioned_at": int | None,
                "provisioning_client_id": str | None,
                "scopes": list[str] | None
            }
            or None if not found or expired
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT encrypted_token, expires_at, flow_type, token_audience,
                       provisioned_at, provisioning_client_id, scopes
                FROM refresh_tokens WHERE user_id = ?
                """,
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            logger.debug(f"No refresh token found for user {user_id}")
            return None

        (
            encrypted_token,
            expires_at,
            flow_type,
            token_audience,
            provisioned_at,
            provisioning_client_id,
            scopes_json,
        ) = row

        # Check expiration
        if expires_at is not None and expires_at < time.time():
            logger.warning(
                f"Refresh token for user {user_id} has expired (expired at {expires_at})"
            )
            await self.delete_refresh_token(user_id)
            return None

        try:
            decrypted_token = self.cipher.decrypt(encrypted_token).decode()
            scopes = json.loads(scopes_json) if scopes_json else None

            logger.debug(
                f"Retrieved refresh token for user {user_id} (flow_type: {flow_type})"
            )

            return {
                "refresh_token": decrypted_token,
                "expires_at": expires_at,
                "flow_type": flow_type or "hybrid",  # Default for existing tokens
                "token_audience": token_audience
                or "nextcloud",  # Default for existing tokens
                "provisioned_at": provisioned_at,
                "provisioning_client_id": provisioning_client_id,
                "scopes": scopes,
            }
        except Exception as e:
            logger.error(f"Failed to decrypt refresh token for user {user_id}: {e}")
            return None

    async def get_refresh_token_by_provisioning_client_id(
        self, provisioning_client_id: str
    ) -> Optional[dict]:
        """
        Retrieve and decrypt refresh token by provisioning_client_id (state parameter).

        This is used to check if an OAuth Flow 2 login completed successfully
        by looking up the refresh token using the state parameter that was generated
        during the authorization request.

        Args:
            provisioning_client_id: OAuth state parameter from the authorization request

        Returns:
            Dictionary with token data or None if not found
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT user_id, encrypted_token, expires_at, flow_type, token_audience,
                       provisioned_at, provisioning_client_id, scopes
                FROM refresh_tokens WHERE provisioning_client_id = ?
                """,
                (provisioning_client_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            logger.debug(
                f"No refresh token found for provisioning_client_id {provisioning_client_id[:16]}..."
            )
            return None

        (
            user_id,
            encrypted_token,
            expires_at,
            flow_type,
            token_audience,
            provisioned_at,
            prov_client_id,
            scopes_json,
        ) = row

        # Check expiration
        if expires_at is not None and expires_at < time.time():
            logger.warning(
                f"Refresh token for provisioning_client_id {provisioning_client_id[:16]}... has expired"
            )
            return None

        try:
            decrypted_token = self.cipher.decrypt(encrypted_token).decode()
            scopes = json.loads(scopes_json) if scopes_json else None

            logger.debug(
                f"Retrieved refresh token for provisioning_client_id {provisioning_client_id[:16]}... (user_id: {user_id})"
            )

            return {
                "user_id": user_id,
                "refresh_token": decrypted_token,
                "expires_at": expires_at,
                "flow_type": flow_type or "hybrid",
                "token_audience": token_audience or "nextcloud",
                "provisioned_at": provisioned_at,
                "provisioning_client_id": prov_client_id,
                "scopes": scopes,
            }
        except Exception as e:
            logger.error(
                f"Failed to decrypt refresh token for provisioning_client_id {provisioning_client_id[:16]}...: {e}"
            )
            return None

    async def delete_refresh_token(self, user_id: str) -> bool:
        """
        Delete refresh token for user.

        Args:
            user_id: User identifier

        Returns:
            True if token was deleted, False if not found
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM refresh_tokens WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            logger.info(f"Deleted refresh token for user {user_id}")
            await self._audit_log(
                event="delete_refresh_token",
                user_id=user_id,
                auth_method="offline_access",
            )
        else:
            logger.debug(f"No refresh token to delete for user {user_id}")

        return deleted

    async def get_all_user_ids(self) -> list[str]:
        """
        Get list of all user IDs with stored refresh tokens.

        Returns:
            List of user IDs
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT user_id FROM refresh_tokens ORDER BY updated_at DESC"
            ) as cursor:
                rows = await cursor.fetchall()

        user_ids = [row[0] for row in rows]
        logger.debug(f"Found {len(user_ids)} users with refresh tokens")
        return user_ids

    async def cleanup_expired_tokens(self) -> int:
        """
        Remove expired refresh tokens from storage.

        Returns:
            Number of tokens deleted
        """
        if not self._initialized:
            await self.initialize()

        now = int(time.time())

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM refresh_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            await db.commit()
            deleted = cursor.rowcount

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired refresh token(s)")

        return deleted

    async def store_oauth_client(
        self,
        client_id: str,
        client_secret: str,
        client_id_issued_at: int,
        client_secret_expires_at: int,
        redirect_uris: list[str],
        registration_access_token: Optional[str] = None,
        registration_client_uri: Optional[str] = None,
    ) -> None:
        """
        Store encrypted OAuth client credentials.

        Args:
            client_id: OAuth client identifier
            client_secret: OAuth client secret (will be encrypted)
            client_id_issued_at: Unix timestamp when client was issued
            client_secret_expires_at: Unix timestamp when secret expires
            redirect_uris: List of redirect URIs
            registration_access_token: RFC 7592 registration token (will be encrypted)
            registration_client_uri: RFC 7592 client management URI
        """
        if not self._initialized:
            await self.initialize()

        # Encrypt sensitive data
        encrypted_secret = self.cipher.encrypt(client_secret.encode())
        encrypted_reg_token = (
            self.cipher.encrypt(registration_access_token.encode())
            if registration_access_token
            else None
        )

        # Serialize redirect_uris as JSON
        redirect_uris_json = json.dumps(redirect_uris)
        now = int(time.time())

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO oauth_clients
                (id, client_id, encrypted_client_secret, client_id_issued_at,
                 client_secret_expires_at, redirect_uris, encrypted_registration_access_token,
                 registration_client_uri, created_at, updated_at)
                VALUES (
                    1, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM oauth_clients WHERE id = 1), ?),
                    ?
                )
                """,
                (
                    client_id,
                    encrypted_secret,
                    client_id_issued_at,
                    client_secret_expires_at,
                    redirect_uris_json,
                    encrypted_reg_token,
                    registration_client_uri,
                    now,
                    now,
                ),
            )
            await db.commit()

        logger.info(
            f"Stored OAuth client credentials (client_id: {client_id[:16]}..., "
            f"expires at {client_secret_expires_at})"
        )

        # Audit log
        await self._audit_log(
            event="store_oauth_client",
            user_id="system",
            auth_method="oauth",
        )

    async def get_oauth_client(self) -> Optional[dict]:
        """
        Retrieve and decrypt OAuth client credentials.

        Returns:
            Dictionary with client credentials, or None if not found or expired:
            {
                "client_id": str,
                "client_secret": str,
                "client_id_issued_at": int,
                "client_secret_expires_at": int,
                "redirect_uris": list[str],
                "registration_access_token": str | None,
                "registration_client_uri": str | None,
            }
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT client_id, encrypted_client_secret, client_id_issued_at,
                       client_secret_expires_at, redirect_uris,
                       encrypted_registration_access_token, registration_client_uri
                FROM oauth_clients WHERE id = 1
                """
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            logger.debug("No OAuth client credentials found in storage")
            return None

        (
            client_id,
            encrypted_secret,
            issued_at,
            expires_at,
            redirect_uris_json,
            encrypted_reg_token,
            reg_client_uri,
        ) = row

        # Check expiration
        if expires_at < time.time():
            logger.warning(
                f"OAuth client has expired (expired at {expires_at}), deleting"
            )
            await self.delete_oauth_client()
            return None

        try:
            # Decrypt sensitive data
            client_secret = self.cipher.decrypt(encrypted_secret).decode()
            reg_token = (
                self.cipher.decrypt(encrypted_reg_token).decode()
                if encrypted_reg_token
                else None
            )

            # Deserialize redirect_uris
            redirect_uris = json.loads(redirect_uris_json)

            logger.debug(
                f"Retrieved OAuth client credentials (client_id: {client_id[:16]}...)"
            )

            return {
                "client_id": client_id,
                "client_secret": client_secret,
                "client_id_issued_at": issued_at,
                "client_secret_expires_at": expires_at,
                "redirect_uris": redirect_uris,
                "registration_access_token": reg_token,
                "registration_client_uri": reg_client_uri,
            }

        except Exception as e:
            logger.error(f"Failed to decrypt OAuth client credentials: {e}")
            return None

    async def delete_oauth_client(self) -> bool:
        """
        Delete OAuth client credentials.

        Returns:
            True if client was deleted, False if not found
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM oauth_clients WHERE id = 1")
            await db.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            logger.info("Deleted OAuth client credentials from storage")
            await self._audit_log(
                event="delete_oauth_client",
                user_id="system",
                auth_method="oauth",
            )
        else:
            logger.debug("No OAuth client credentials to delete")

        return deleted

    async def has_oauth_client(self) -> bool:
        """
        Check if OAuth client credentials exist (and are not expired).

        Returns:
            True if valid client exists, False otherwise
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT client_secret_expires_at FROM oauth_clients WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            return False

        expires_at = row[0]
        return expires_at >= time.time()

    async def _audit_log(
        self,
        event: str,
        user_id: str,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        auth_method: Optional[str] = None,
    ) -> None:
        """
        Log operation to audit log.

        Args:
            event: Event name (e.g., "store_refresh_token", "token_refresh")
            user_id: User identifier
            resource_type: Resource type (e.g., "note", "file")
            resource_id: Resource identifier
            auth_method: Authentication method used
        """
        import socket

        hostname = socket.gethostname()
        timestamp = int(time.time())

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO audit_logs
                (timestamp, event, user_id, resource_type, resource_id, auth_method, hostname)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    event,
                    user_id,
                    resource_type,
                    resource_id,
                    auth_method,
                    hostname,
                ),
            )
            await db.commit()

    async def get_audit_logs(
        self,
        user_id: Optional[str] = None,
        since: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Retrieve audit logs.

        Args:
            user_id: Filter by user ID (optional)
            since: Filter by timestamp (Unix epoch, optional)
            limit: Maximum number of logs to return

        Returns:
            List of audit log entries
        """
        if not self._initialized:
            await self.initialize()

        query = "SELECT * FROM audit_logs WHERE 1=1"
        params = []

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        if since:
            query += " AND timestamp >= ?"
            params.append(since)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()

        return [dict(row) for row in rows]

    async def store_oauth_session(
        self,
        session_id: str,
        client_redirect_uri: str,
        state: Optional[str] = None,
        code_challenge: Optional[str] = None,
        code_challenge_method: Optional[str] = None,
        mcp_authorization_code: Optional[str] = None,
        client_id: Optional[str] = None,
        flow_type: str = "hybrid",
        is_provisioning: bool = False,
        requested_scopes: Optional[str] = None,
        ttl_seconds: int = 600,  # 10 minutes
    ) -> None:
        """
        Store OAuth session for ADR-004 Progressive Consent.

        Args:
            session_id: Unique session identifier
            client_redirect_uri: Client's localhost redirect URI
            state: CSRF protection state parameter
            code_challenge: PKCE code challenge
            code_challenge_method: PKCE method (S256)
            mcp_authorization_code: Pre-generated MCP authorization code
            client_id: Client identifier (for Flow 1)
            flow_type: Type of flow ('hybrid', 'flow1', 'flow2')
            is_provisioning: Whether this is a Flow 2 provisioning session
            requested_scopes: Requested OAuth scopes
            ttl_seconds: Session TTL in seconds
        """
        if not self._initialized:
            await self.initialize()

        now = int(time.time())
        expires_at = now + ttl_seconds

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO oauth_sessions
                (session_id, client_id, client_redirect_uri, state, code_challenge,
                 code_challenge_method, mcp_authorization_code, flow_type,
                 is_provisioning, requested_scopes, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    client_id,
                    client_redirect_uri,
                    state,
                    code_challenge,
                    code_challenge_method,
                    mcp_authorization_code,
                    flow_type,
                    is_provisioning,
                    requested_scopes,
                    now,
                    expires_at,
                ),
            )
            await db.commit()

        logger.debug(f"Stored OAuth session {session_id} (expires in {ttl_seconds}s)")

    async def get_oauth_session(self, session_id: str) -> Optional[dict]:
        """
        Retrieve OAuth session by session ID.

        Returns:
            Session dictionary or None if not found/expired
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM oauth_sessions WHERE session_id = ?", (session_id,)
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            return None

        session = dict(row)

        # Check expiration
        if session["expires_at"] < time.time():
            logger.debug(f"OAuth session {session_id} has expired")
            await self.delete_oauth_session(session_id)
            return None

        return session

    async def get_oauth_session_by_mcp_code(
        self, mcp_authorization_code: str
    ) -> Optional[dict]:
        """
        Retrieve OAuth session by MCP authorization code.

        Returns:
            Session dictionary or None if not found/expired
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM oauth_sessions WHERE mcp_authorization_code = ?",
                (mcp_authorization_code,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            return None

        session = dict(row)

        # Check expiration
        if session["expires_at"] < time.time():
            logger.debug(
                f"OAuth session with MCP code {mcp_authorization_code[:16]}... has expired"
            )
            await self.delete_oauth_session(session["session_id"])
            return None

        return session

    async def update_oauth_session(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        idp_access_token: Optional[str] = None,
        idp_refresh_token: Optional[str] = None,
    ) -> bool:
        """
        Update OAuth session with IdP token data.

        Returns:
            True if session was updated, False if not found
        """
        if not self._initialized:
            await self.initialize()

        update_fields = []
        params = []

        if user_id is not None:
            update_fields.append("user_id = ?")
            params.append(user_id)

        if idp_access_token is not None:
            update_fields.append("idp_access_token = ?")
            params.append(idp_access_token)

        if idp_refresh_token is not None:
            update_fields.append("idp_refresh_token = ?")
            params.append(idp_refresh_token)

        if not update_fields:
            return False

        params.append(session_id)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"""
                UPDATE oauth_sessions
                SET {", ".join(update_fields)}
                WHERE session_id = ?
                """,
                params,
            )
            await db.commit()
            updated = cursor.rowcount > 0

        if updated:
            logger.debug(f"Updated OAuth session {session_id}")

        return updated

    async def delete_oauth_session(self, session_id: str) -> bool:
        """
        Delete OAuth session.

        Returns:
            True if session was deleted, False if not found
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM oauth_sessions WHERE session_id = ?", (session_id,)
            )
            await db.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            logger.debug(f"Deleted OAuth session {session_id}")

        return deleted

    async def cleanup_expired_sessions(self) -> int:
        """
        Remove expired OAuth sessions from storage.

        Returns:
            Number of sessions deleted
        """
        if not self._initialized:
            await self.initialize()

        now = int(time.time())

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM oauth_sessions WHERE expires_at < ?", (now,)
            )
            await db.commit()
            deleted = cursor.rowcount

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired OAuth session(s)")

        return deleted

    # ============================================================================
    # Webhook Registration Tracking (both BasicAuth and OAuth modes)
    # ============================================================================

    async def store_webhook(self, webhook_id: int, preset_id: str) -> None:
        """
        Store registered webhook ID for tracking.

        Args:
            webhook_id: Nextcloud webhook ID
            preset_id: Preset identifier (e.g., "notes_sync", "calendar_sync")
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO registered_webhooks (webhook_id, preset_id, created_at) VALUES (?, ?, ?)",
                (webhook_id, preset_id, time.time()),
            )
            await db.commit()

        logger.debug(f"Stored webhook {webhook_id} for preset '{preset_id}'")

    async def get_webhooks_by_preset(self, preset_id: str) -> list[int]:
        """
        Get all webhook IDs registered for a preset.

        Args:
            preset_id: Preset identifier

        Returns:
            List of webhook IDs
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT webhook_id FROM registered_webhooks WHERE preset_id = ?",
                (preset_id,),
            )
            rows = await cursor.fetchall()

        return [row[0] for row in rows]

    async def delete_webhook(self, webhook_id: int) -> bool:
        """
        Remove webhook from tracking.

        Args:
            webhook_id: Nextcloud webhook ID to remove

        Returns:
            True if webhook was deleted, False if not found
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM registered_webhooks WHERE webhook_id = ?", (webhook_id,)
            )
            await db.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            logger.debug(f"Deleted webhook {webhook_id} from tracking")

        return deleted

    async def list_all_webhooks(self) -> list[dict]:
        """
        List all tracked webhooks with metadata.

        Returns:
            List of webhook dictionaries with keys: webhook_id, preset_id, created_at
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT webhook_id, preset_id, created_at FROM registered_webhooks ORDER BY created_at DESC"
            )
            rows = await cursor.fetchall()

        return [
            {"webhook_id": row[0], "preset_id": row[1], "created_at": row[2]}
            for row in rows
        ]

    async def clear_preset_webhooks(self, preset_id: str) -> int:
        """
        Delete all webhooks for a preset (bulk operation).

        Args:
            preset_id: Preset identifier

        Returns:
            Number of webhooks deleted
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM registered_webhooks WHERE preset_id = ?", (preset_id,)
            )
            await db.commit()
            deleted = cursor.rowcount

        if deleted > 0:
            logger.debug(f"Cleared {deleted} webhook(s) for preset '{preset_id}'")

        return deleted


async def generate_encryption_key() -> str:
    """
    Generate a new Fernet encryption key.

    Returns:
        Base64-encoded encryption key suitable for TOKEN_ENCRYPTION_KEY env var
    """
    return Fernet.generate_key().decode()


# Example usage
if __name__ == "__main__":
    import asyncio

    async def main():
        # Generate a key for testing
        key = await generate_encryption_key()
        print(f"Generated encryption key: {key}")
        print(f"Set this in your environment: export TOKEN_ENCRYPTION_KEY='{key}'")

    asyncio.run(main())
