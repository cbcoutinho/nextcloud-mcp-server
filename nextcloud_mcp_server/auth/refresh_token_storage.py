"""
Refresh Token Storage for ADR-002 Tier 1: Offline Access

Securely stores and manages user refresh tokens for background operations.
Tokens are encrypted at rest using Fernet symmetric encryption.
"""

import base64
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

        try:
            encryption_key = base64.b64decode(encryption_key_b64)
        except Exception as e:
            raise ValueError(
                f"Invalid TOKEN_ENCRYPTION_KEY: {e}. "
                "Must be a base64-encoded Fernet key."
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
