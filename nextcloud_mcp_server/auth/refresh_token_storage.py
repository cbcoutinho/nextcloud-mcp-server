"""
Refresh Token Storage for ADR-002 Tier 1: Offline Access

Securely stores and manages user refresh tokens for background operations.
Tokens are encrypted at rest using Fernet symmetric encryption.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import aiosqlite
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


class RefreshTokenStorage:
    """Securely store and manage user refresh tokens"""

    def __init__(self, db_path: str, encryption_key: bytes):
        """
        Initialize refresh token storage.

        Args:
            db_path: Path to SQLite database file
            encryption_key: Fernet encryption key (32 bytes, base64-encoded)
        """
        self.db_path = db_path
        self.cipher = Fernet(encryption_key)
        self._initialized = False

    @classmethod
    def from_env(cls) -> "RefreshTokenStorage":
        """
        Create storage instance from environment variables.

        Environment variables:
            TOKEN_STORAGE_DB: Path to database file (default: /app/data/tokens.db)
            TOKEN_ENCRYPTION_KEY: Base64-encoded Fernet key

        Returns:
            RefreshTokenStorage instance

        Raises:
            ValueError: If TOKEN_ENCRYPTION_KEY is not set
        """
        db_path = os.getenv("TOKEN_STORAGE_DB", "/app/data/tokens.db")
        encryption_key_b64 = os.getenv("TOKEN_ENCRYPTION_KEY")

        if not encryption_key_b64:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY environment variable is required. "
                "Generate one with: python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            )

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
                    updated_at INTEGER NOT NULL
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

            # OAuth flow sessions (ADR-004 Hybrid Flow)
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
                    expires_at INTEGER NOT NULL
                )
                """
            )

            # Create index for MCP authorization code lookups
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_oauth_sessions_mcp_code "
                "ON oauth_sessions(mcp_authorization_code)"
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
    ) -> None:
        """
        Store encrypted refresh token for user.

        Args:
            user_id: User identifier (from OIDC 'sub' claim)
            refresh_token: Refresh token to store
            expires_at: Token expiration timestamp (Unix epoch), if known

        """
        if not self._initialized:
            await self.initialize()

        encrypted_token = self.cipher.encrypt(refresh_token.encode())
        now = int(time.time())

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO refresh_tokens
                (user_id, encrypted_token, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, COALESCE((SELECT created_at FROM refresh_tokens WHERE user_id = ?), ?), ?)
                """,
                (user_id, encrypted_token, expires_at, user_id, now, now),
            )
            await db.commit()

        logger.info(
            f"Stored refresh token for user {user_id}"
            + (f" (expires at {expires_at})" if expires_at else "")
        )

        # Audit log
        await self._audit_log(
            event="store_refresh_token",
            user_id=user_id,
            auth_method="offline_access",
        )

    async def get_refresh_token(self, user_id: str) -> Optional[str]:
        """
        Retrieve and decrypt refresh token for user.

        Args:
            user_id: User identifier

        Returns:
            Decrypted refresh token, or None if not found or expired
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT encrypted_token, expires_at FROM refresh_tokens WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            logger.debug(f"No refresh token found for user {user_id}")
            return None

        encrypted_token, expires_at = row

        # Check expiration
        if expires_at is not None and expires_at < time.time():
            logger.warning(
                f"Refresh token for user {user_id} has expired (expired at {expires_at})"
            )
            await self.delete_refresh_token(user_id)
            return None

        try:
            decrypted_token = self.cipher.decrypt(encrypted_token).decode()
            logger.debug(f"Retrieved refresh token for user {user_id}")
            return decrypted_token
        except Exception as e:
            logger.error(f"Failed to decrypt refresh token for user {user_id}: {e}")
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
        ttl_seconds: int = 600,  # 10 minutes
    ) -> None:
        """
        Store OAuth session for Hybrid Flow (ADR-004).

        Args:
            session_id: Unique session identifier
            client_redirect_uri: Client's localhost redirect URI
            state: CSRF protection state parameter
            code_challenge: PKCE code challenge
            code_challenge_method: PKCE method (S256)
            mcp_authorization_code: Pre-generated MCP authorization code
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
                (session_id, client_redirect_uri, state, code_challenge,
                 code_challenge_method, mcp_authorization_code, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    client_redirect_uri,
                    state,
                    code_challenge,
                    code_challenge_method,
                    mcp_authorization_code,
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
