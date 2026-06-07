"""
Persistent Storage for MCP Server State

This module provides SQL-backed storage for multiple concerns across both
BasicAuth and OAuth authentication modes. The default backend is SQLite
(file-based or per-process tempfile); set ``DATABASE_URL`` to a
``postgresql+asyncpg://...`` URL for HA k8s deployments where pods need
to be stateless. See :doc:`ADR-026 </docs/ADR-026-pluggable-database-backend>`
for the design.

Concerns covered:

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

import hashlib
import importlib.util
import json
import logging
import os
import socket
import sqlite3
import time
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import sqlalchemy as sa
from anyio import to_thread
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from nextcloud_mcp_server.config import (
    get_database_ssl,
    get_database_url,
    is_ephemeral_token_db,
    is_sqlite_url,
    mask_db_password,
)
from nextcloud_mcp_server.migrations import stamp_database, upgrade_database
from nextcloud_mcp_server.observability.metrics import record_db_operation

logger = logging.getLogger(__name__)


# Stable 64-bit signed integer used for the Postgres advisory-lock that
# serializes concurrent Alembic migrations across pods (ADR-026 →
# "Concurrent migrations"). Derived from a SHA-256 of a project-scoped
# string so we can't collide with other apps sharing the same DB.
_MIGRATION_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"nextcloud-mcp-server:migrations").digest()[:8],
    "big",
    signed=True,
)


def _qmark_to_named(sql: str) -> tuple[str, list[str]]:
    """Rewrite ``?`` positional placeholders to ``:p0, :p1, ...`` named binds.

    SQLAlchemy's :func:`text` only supports named bind parameters, so the
    aiosqlite-style call sites (which use ``?``) are translated as they
    cross the shim. The rewriter preserves ``?`` characters inside SQL
    string literals; comments are not currently respected but the storage
    layer doesn't put ``?`` inside comments.
    """
    out: list[str] = []
    names: list[str] = []
    i = 0
    in_str = False
    quote = ""
    n = 0
    while i < len(sql):
        ch = sql[i]
        if in_str:
            out.append(ch)
            if ch == quote:
                # SQL string escapes ('' or "") — stay in string mode.
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(quote)
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "?":
            name = f"p{n}"
            out.append(f":{name}")
            names.append(name)
            n += 1
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), names


class _Row:
    """Hybrid tuple/dict row mirroring ``aiosqlite.Row`` semantics.

    The legacy SQLite-direct call sites use a mix of access patterns:
    positional unpacking (``a, b, c = row``), indexed access (``row[0]``),
    and dict-like access (``row["col"]``, ``dict(row)``) when
    ``db.row_factory = aiosqlite.Row`` is set. To avoid touching every call
    site, every row returned by :class:`_Cursor` is wrapped in this hybrid
    object so all three patterns keep working.
    """

    __slots__ = ("_values", "_mapping")

    def __init__(self, values: tuple, mapping: dict) -> None:
        self._values = values
        self._mapping = mapping

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self):
        return self._mapping.keys()

    def values(self):
        return self._mapping.values()

    def items(self):
        return self._mapping.items()


def _wrap_row(row) -> _Row | None:
    if row is None:
        return None
    # ``row._mapping`` is the documented public RowMapping accessor in
    # SQLAlchemy 2.x (the leading underscore is historical); it returns
    # a column-name → value mapping that survives the row being
    # tuple-iterated. See SQLAlchemy 2.x ``Row.mapping`` docs.
    return _Row(tuple(row), dict(row._mapping))


def _describe_ssl_arg(ssl_arg: object) -> str:
    """Render the ``ssl`` value for the startup log line.

    Split out of the engine factory to avoid a nested-ternary
    SonarQube finding (``S3358``) and to make the cases readable.
    """
    if ssl_arg is False:
        return "disabled"
    if isinstance(ssl_arg, bool):
        return "verify-full (system CAs)"
    return "custom CA bundle"


def _wrap_rows(rows) -> list[_Row]:
    """Wrap a list of SQLAlchemy rows; iterator never yields ``None``."""
    return [_Row(tuple(r), dict(r._mapping)) for r in rows]


class _Cursor:
    """aiosqlite-compatible cursor view over a SQLAlchemy CursorResult.

    Existing storage methods iterate cursors via ``async with db.execute(...)
    as cursor: row = await cursor.fetchone()``. SQLAlchemy returns a
    synchronous :class:`Result` from an async ``execute``; this shim adds the
    async context-manager / async-fetch surface so call sites are unchanged.

    ``rowcount`` is captured eagerly at construction time. ``lastrowid`` is
    intentionally NOT exposed: accessing ``CursorResult.lastrowid`` on the
    asyncpg dialect consumes the result buffer, which would silently turn
    every subsequent ``fetchall()`` into an empty list (a real bug hit
    during the Postgres port).
    """

    __slots__ = ("_result", "rowcount")

    def __init__(self, result: sa.CursorResult) -> None:
        self._result = result
        # ``rowcount`` is -1 for SELECTs in SQLAlchemy; existing code only
        # reads it after writes (DELETE/UPDATE) where it is accurate.
        self.rowcount = result.rowcount

    async def fetchone(self) -> _Row | None:
        return _wrap_row(self._result.fetchone())

    async def fetchall(self) -> list[_Row]:
        return _wrap_rows(self._result.fetchall())

    # Python's async-context-manager protocol *requires* ``__aenter__`` and
    # ``__aexit__`` to be coroutines even when the body has nothing to
    # await; dropping ``async`` would break ``async with _Cursor(...)``.
    # The bare ``# NOSONAR`` markers below silence ``python:S7503``
    # ("async function with no await") for that protocol-mandated reason.
    async def __aenter__(self) -> "_Cursor":  # NOSONAR
        return self

    async def __aexit__(self, *exc: object) -> None:  # NOSONAR
        # SQLAlchemy Result closes when the connection closes; no-op here.
        return None


class _ExecuteCtx:
    """Hybrid awaitable + async context manager for ``db.execute(...)``.

    Aiosqlite call sites use both forms interchangeably::

        cursor = await db.execute(sql, params)
        async with db.execute(sql, params) as cursor: ...

    so the return value must be awaitable (resolves to a cursor) AND a
    one-shot async context manager (executes on ``__aenter__`` and returns
    the cursor). This wrapper provides both surfaces without executing the
    SQL twice — the cursor is cached after the first resolution.
    """

    __slots__ = ("_conn", "_sql", "_params", "_cursor")

    def __init__(self, conn: AsyncConnection, sql: str, params: tuple | list) -> None:
        self._conn = conn
        self._sql = sql
        self._params = params
        self._cursor: _Cursor | None = None

    async def _resolve(self) -> _Cursor:
        if self._cursor is not None:
            return self._cursor
        text_sql, names = _qmark_to_named(self._sql)
        if len(names) != len(self._params):
            raise ValueError(
                f"Placeholder count mismatch: SQL has {len(names)} '?' "
                f"but got {len(self._params)} params"
            )
        bind = dict(zip(names, self._params, strict=True))
        result = await self._conn.execute(sa.text(text_sql), bind)
        self._cursor = _Cursor(result)
        return self._cursor

    def __await__(self):
        return self._resolve().__await__()

    async def __aenter__(self) -> _Cursor:
        return await self._resolve()

    async def __aexit__(self, *exc: object) -> None:
        return None


class _DBConn:
    """aiosqlite-compatible wrapper around a SQLAlchemy AsyncConnection.

    Provides ``execute`` (with ``?`` placeholders, returning a hybrid
    awaitable/context-manager :class:`_ExecuteCtx`) and ``commit`` so the
    existing storage method bodies need no churn beyond swapping the
    connection context-manager. Wraps results in :class:`_Cursor` for the
    fetchone/fetchall/rowcount surface the call sites already use.

    ``row_factory`` is accepted as a setter for source compatibility with
    aiosqlite call sites but is ignored: every row is already wrapped in
    :class:`_Row` so dict-style access works unconditionally.
    """

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn
        self.row_factory = None  # set by aiosqlite-shaped call sites; ignored

    def execute(self, sql: str, params: tuple | list = ()) -> _ExecuteCtx:
        return _ExecuteCtx(self._conn, sql, params)

    async def commit(self) -> None:
        await self._conn.commit()


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

    def __init__(
        self,
        database_url: str | None = None,
        encryption_key: bytes | None = None,
        *,
        db_path: str | None = None,
    ):
        """
        Initialize persistent storage.

        Args:
            database_url: SQLAlchemy URL (``sqlite+aiosqlite:///...`` or
                ``postgresql+asyncpg://...``). When omitted, falls back to
                :func:`get_database_url` (honors ``DATABASE_URL`` env, then
                ``TOKEN_STORAGE_DB``).
            encryption_key: Optional Fernet encryption key (32 bytes, base64-encoded).
                Required for token storage operations, not required for webhook tracking.
            db_path: Deprecated SQLite-only constructor argument retained for
                tests that pass a tempfile path. Internally converted to
                ``sqlite+aiosqlite:///{db_path}``.
        """
        if database_url is None and db_path is not None:
            database_url = f"sqlite+aiosqlite:///{db_path}"
        if database_url is None:
            database_url = get_database_url()
        self.database_url = database_url
        # Legacy attribute retained for sqlite-only code paths (file perms,
        # ephemeral tempfile detection, log messages). Empty string for
        # non-sqlite URLs so accidental file ops fail loudly. We delegate
        # the parsing to SQLAlchemy's ``make_url`` rather than splitting
        # on ``///`` — same result for both 3-slash (relative) and
        # 4-slash (absolute) SQLite URLs, plus correct handling of the
        # in-memory ``:memory:`` form (``.database`` is ``None`` there).
        if is_sqlite_url(database_url):
            from sqlalchemy.engine.url import make_url  # noqa: PLC0415

            self.db_path = make_url(database_url).database or ""
        else:
            self.db_path = ""
        self.cipher = Fernet(encryption_key) if encryption_key else None
        self.engine: AsyncEngine | None = None
        self._dialect: str = "unknown"
        self._initialized = False

    @classmethod
    def from_env(cls) -> "RefreshTokenStorage":
        """
        Create storage instance from environment variables.

        Environment variables:
            DATABASE_URL: SQLAlchemy URL for any supported backend. Wins
                over ``TOKEN_STORAGE_DB`` when set. Use
                ``postgresql+asyncpg://user:pw@host/db`` for HA k8s
                deployments. See ADR-026.
            TOKEN_STORAGE_DB: Legacy SQLite-only path. If unset and
                ``DATABASE_URL`` is also unset, a per-process tempfile is
                allocated and deleted at interpreter exit — tokens are
                ephemeral and wiped on restart.
            TOKEN_ENCRYPTION_KEY: Optional base64-encoded Fernet key (required for token storage)

        Returns:
            RefreshTokenStorage instance

        Note:
            If TOKEN_ENCRYPTION_KEY is not set, token storage operations will fail,
            but webhook tracking will still work.
        """
        database_url = get_database_url()
        if is_sqlite_url(database_url):
            sqlite_path = database_url.split("///", 1)[1]
            if is_ephemeral_token_db(sqlite_path):
                logger.info(
                    "Using ephemeral token storage at %s "
                    "(set DATABASE_URL or TOKEN_STORAGE_DB to persist tokens across restarts)",
                    sqlite_path,
                )
        else:
            logger.info(
                "Using centralized token storage at %s", mask_db_password(database_url)
            )
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

        return cls(database_url=database_url, encryption_key=encryption_key)

    async def initialize(self) -> None:
        """
        Initialize database schema using Alembic migrations.

        This method handles three scenarios:
        1. New database: Run migrations from scratch
        2. Pre-Alembic database: Stamp with initial revision (no changes)
        3. Alembic-managed database: Upgrade to latest version

        Raises:
            RuntimeError: when the underlying SQLite library is older than
                3.35, which is required for ``DELETE ... RETURNING`` used by
                ``delete_browser_session`` (PR #758 round-5 review low 2).
                Ubuntu 20.04 ships SQLite 3.31, so deployers on that
                baseline must upgrade or use a newer Python image.
        """
        if self._initialized:
            return

        is_sqlite = is_sqlite_url(self.database_url)

        if is_sqlite and sqlite3.sqlite_version_info < (3, 35):
            raise RuntimeError(
                "SQLite >= 3.35 is required (DELETE ... RETURNING is used "
                "by delete_browser_session); detected "
                f"{sqlite3.sqlite_version}. Upgrade SQLite or use a Python "
                "image with a newer bundled libsqlite3."
            )

        if is_sqlite:
            # File-permission hardening + parent dir creation is sqlite-only;
            # centralized backends manage their own filesystem.
            db_dir = Path(self.db_path).parent
            db_dir.mkdir(parents=True, exist_ok=True)
            if Path(self.db_path).exists():
                os.chmod(self.db_path, 0o600)

        # Create the shared async engine for the chosen backend. Both
        # SQLite and Postgres use NullPool (per-call connections, no
        # cross-loop bookkeeping). SQLite mirrors the prior
        # aiosqlite-direct behavior; see ``_build_postgres_engine`` for
        # the Postgres rationale.
        if is_sqlite:
            self.engine = create_async_engine(
                self.database_url,
                poolclass=NullPool,
                connect_args={"check_same_thread": False},
                future=True,
            )
        else:
            self.engine = self._build_postgres_engine()
        self._dialect = self.engine.dialect.name

        # Check database state with the SQLAlchemy inspector so the legacy
        # ``sqlite_master`` lookup works against either backend.
        def _inspect(sync_conn: sa.Connection) -> tuple[bool, bool]:
            insp = sa.inspect(sync_conn)
            tables = set(insp.get_table_names())
            return ("alembic_version" in tables), ("refresh_tokens" in tables)

        # Hold the advisory lock across BOTH the inspect and the migration
        # call so two pods racing the rolling-update can't both see "no
        # alembic_version" and both try to run from scratch. The lock is a
        # no-op on SQLite (file-level locking serializes writes natively).
        async with self._migration_lock():
            async with self.engine.connect() as conn:
                has_alembic, has_schema = await conn.run_sync(_inspect)

            if not has_alembic:
                if has_schema:
                    logger.info(
                        "Detected pre-Alembic database at %s, stamping with initial revision",
                        mask_db_password(self.database_url),
                    )
                    await to_thread.run_sync(stamp_database, self.database_url, "001")
                    logger.info(
                        "Pre-Alembic database stamped successfully. "
                        "Future schema changes will use migrations."
                    )
                else:
                    logger.info(
                        "Initializing new database at %s with migrations",
                        mask_db_password(self.database_url),
                    )
                    await to_thread.run_sync(
                        upgrade_database, self.database_url, "head"
                    )
                    logger.info("Database initialized with migrations")
            else:
                await to_thread.run_sync(upgrade_database, self.database_url, "head")
                logger.info("Database upgraded to latest version")

        if is_sqlite:
            os.chmod(self.db_path, 0o600)

        self._initialized = True
        logger.info(
            "Initialized refresh token storage at %s",
            mask_db_password(self.database_url),
        )

    def _build_postgres_engine(self) -> AsyncEngine:
        """Construct the AsyncEngine for a Postgres ``DATABASE_URL``.

        Split out from :meth:`initialize` so cognitive complexity stays
        under the SonarQube ``S3776`` threshold and so a future
        engine-arg unit test has a single seam to mock.

        Uses :class:`NullPool` (one fresh asyncpg connection per
        checkout, no caching). The original ADR-026 design used a
        small bounded ``QueuePool`` with ``pool_pre_ping=True``, but
        that combination is unsafe under the server's anyio task
        layout: cached asyncpg connections are bound to the event
        loop they were opened on, and a checkout from a task running
        under a different anyio TaskGroup / loop triggers
        ``RuntimeError: got Future attached to a different loop`` on
        the pre-ping probe (and then the pool closes the connection
        with another ``Event loop is closed`` while cleaning up).
        Observed in production against shared-postgres on cloudfleet,
        where the background ``vector.oauth_sync.user_manager_task``
        and the request-path code paths share an engine across loops.

        NullPool sidesteps the entire class of bugs: every
        ``engine.connect()`` opens a fresh asyncpg connection in the
        caller's current loop, and disposes it on close. asyncpg
        connection setup is cheap (~5 ms LAN, single round-trip when
        the server is local) so the throughput cost is negligible for
        the MCP server's traffic shape (low-concurrency, bursty).
        ``DATABASE_POOL_SIZE`` / ``DATABASE_MAX_OVERFLOW`` are still
        accepted for backward compat but no longer have an effect on
        the Postgres backend — they were never propagated to SQLite,
        which has always used NullPool.
        """
        # asyncpg ships as an optional PyPI extra (`[postgres]`) so the
        # default `pip install nextcloud-mcp-server` audience doesn't
        # pull in the C extension. The Docker image bundles it. Surface
        # a clear actionable error when the driver is missing rather
        # than the generic ``ModuleNotFoundError`` SQLAlchemy emits.
        if "+asyncpg" in self.database_url.lower() and (
            importlib.util.find_spec("asyncpg") is None
        ):
            raise RuntimeError(
                "DATABASE_URL points at Postgres via asyncpg but the "
                "'asyncpg' driver is not installed. Install with "
                "`pip install nextcloud-mcp-server[postgres]` or use "
                "the Docker image, which bundles it. See ADR-026."
            )

        # Conditionally pass TLS config through to asyncpg. When
        # ``get_database_ssl()`` returns None we omit ``ssl`` entirely
        # so asyncpg's default (``prefer``) applies — keeps
        # cluster-local Postgres without TLS working out of the box.
        connect_args: dict[str, object] = {}
        ssl_arg = get_database_ssl()
        if ssl_arg is not None:
            connect_args["ssl"] = ssl_arg
            logger.info("Postgres backend TLS: %s", _describe_ssl_arg(ssl_arg))

        engine = create_async_engine(
            self.database_url,
            poolclass=NullPool,
            connect_args=connect_args,
            future=True,
        )
        logger.info(
            "Postgres engine ready: NullPool (one connection per "
            "checkout, see ADR-026 § 'Connection pool')"
        )
        return engine

    async def close(self) -> None:
        """Dispose the underlying AsyncEngine on shutdown.

        With ``NullPool`` the dispose call has no idle pool to drain,
        but it still cleanly tears down any in-flight asyncpg
        connections held by active checkouts so shutdown hooks don't
        leave dangling transports behind. Idempotent: safe to call
        from any number of shutdown hooks.
        """
        if self.engine is None:
            return
        await self.engine.dispose()
        self.engine = None
        self._initialized = False
        logger.info("Disposed token storage engine")

    @asynccontextmanager
    async def _migration_lock(self):
        """Serialize concurrent Alembic migrations across pods (ADR-026).

        Without this, two pods rolling-updating at the same time can race
        Alembic's version-table UPDATE and both try to apply migrations
        from scratch — the second one crashes with "relation already
        exists". On Postgres we acquire a session-level
        :func:`pg_advisory_lock` so the second pod blocks until the
        first finishes. SQLite serializes writes via its own file lock
        and needs no extra coordination, so this is a no-op there.

        The lock is held on a separate connection from the engine pool
        so it survives the worker-thread ``to_thread.run_sync`` call
        that actually runs Alembic.
        """
        assert self.engine is not None, "engine must be built before migration lock"
        if is_sqlite_url(self.database_url):
            yield
            return

        async with self.engine.connect() as conn:
            await conn.execute(
                sa.text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": _MIGRATION_LOCK_ID},
            )
            logger.debug(
                "Acquired Postgres advisory migration lock %s", _MIGRATION_LOCK_ID
            )
            try:
                yield
            finally:
                await conn.execute(
                    sa.text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _MIGRATION_LOCK_ID},
                )
                logger.debug(
                    "Released Postgres advisory migration lock %s",
                    _MIGRATION_LOCK_ID,
                )

    @asynccontextmanager
    async def _db(self):
        """Open a backend-agnostic connection.

        Yields a :class:`_DBConn` that mimics aiosqlite's API (``execute`` with
        ``?`` placeholders, ``commit``, cursor with ``fetchone`` /
        ``fetchall`` / ``rowcount``) so the existing storage method bodies
        work against either SQLite or Postgres without per-call rewrites.
        """
        assert self.engine is not None, "RefreshTokenStorage.initialize() not called"
        async with self.engine.connect() as conn:
            yield _DBConn(conn)

    def acquire(self) -> AbstractAsyncContextManager["_DBConn"]:
        """Public alias for :meth:`_db`: a backend-agnostic connection cm.

        Lets sibling stores (e.g. :class:`UsageEventStore`) reuse this
        instance's engine, NullPool, and ``_DBConn`` shim without reaching
        into the underscored internal. Use as ``async with storage.acquire()
        as db:``.
        """
        return self._db()

    @property
    def dialect(self) -> str:
        """Backend dialect name ("sqlite" / "postgresql"), or "unknown" pre-init."""
        return self._dialect

    async def store_refresh_token(
        self,
        user_id: str,
        refresh_token: str,
        expires_at: int | None = None,
        flow_type: str = "hybrid",
        token_audience: str = "nextcloud",
        provisioning_client_id: str | None = None,
        scopes: list[str] | None = None,
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

        # ``assert`` is stripped under ``python -O``, which would silently
        # turn a missing TOKEN_ENCRYPTION_KEY into an ``AttributeError`` on
        # the next ``self.cipher.encrypt(...)``. Raise explicitly instead
        # (PR #758 round-4 review medium 1).
        if self.cipher is None:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY is not set — token storage operations unavailable"
            )
        encrypted_token = self.cipher.encrypt(refresh_token.encode())
        now = int(time.time())
        scopes_json = json.dumps(scopes) if scopes else None

        # For Flow 2, set provisioned_at timestamp
        provisioned_at = now if flow_type == "flow2" else None

        start_time = time.time()
        try:
            async with self._db() as db:
                # ON CONFLICT DO UPDATE preserves ``created_at`` (it's not
                # listed in the update clause) so the original
                # COALESCE(...)-based INSERT OR REPLACE semantics are kept.
                await db.execute(
                    """
                    INSERT INTO refresh_tokens
                    (user_id, encrypted_token, expires_at, created_at, updated_at,
                     flow_type, token_audience, provisioned_at, provisioning_client_id, scopes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id) DO UPDATE SET
                        encrypted_token = EXCLUDED.encrypted_token,
                        expires_at = EXCLUDED.expires_at,
                        updated_at = EXCLUDED.updated_at,
                        flow_type = EXCLUDED.flow_type,
                        token_audience = EXCLUDED.token_audience,
                        provisioned_at = EXCLUDED.provisioned_at,
                        provisioning_client_id = EXCLUDED.provisioning_client_id,
                        scopes = EXCLUDED.scopes
                    """,
                    (
                        user_id,
                        encrypted_token,
                        expires_at,
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
            record_db_operation(self._dialect, "insert", duration, "success")

            logger.info(
                f"Stored refresh token for user {user_id}"
                + (f" (expires at {expires_at})" if expires_at else "")
            )
        except Exception:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "insert", duration, "error")
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

        async with self._db() as db:
            await db.execute(
                """
                UPDATE refresh_tokens
                SET user_profile = ?, profile_cached_at = ?
                WHERE user_id = ?
                """,
                (profile_json, now, user_id),
            )
            await db.commit()

        logger.debug("Cached user profile for %s", user_id)

    async def get_user_profile(self, user_id: str) -> dict[str, Any] | None:
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

        async with self._db() as db:
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

    async def get_refresh_token(self, user_id: str) -> dict | None:
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

        # ``assert`` is stripped under ``python -O``, which would silently
        # turn a missing TOKEN_ENCRYPTION_KEY into an ``AttributeError`` on
        # the next ``self.cipher.encrypt(...)``. Raise explicitly instead
        # (PR #758 round-4 review medium 1).
        if self.cipher is None:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY is not set — token storage operations unavailable"
            )

        start_time = time.time()
        try:
            async with self._db() as db:
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
                logger.debug("No refresh token found for user %s", user_id)
                duration = time.time() - start_time
                record_db_operation(self._dialect, "select", duration, "success")
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
                    "Refresh token for user %s has expired (expired at %s)",
                    user_id,
                    expires_at,
                )
                await self.delete_refresh_token(user_id)
                duration = time.time() - start_time
                record_db_operation(self._dialect, "select", duration, "success")
                return None

            decrypted_token = self.cipher.decrypt(encrypted_token).decode()
            scopes = json.loads(scopes_json) if scopes_json else None

            logger.debug(
                "Retrieved refresh token for user %s (flow_type: %s)",
                user_id,
                flow_type,
            )

            duration = time.time() - start_time
            record_db_operation(self._dialect, "select", duration, "success")

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
            duration = time.time() - start_time
            record_db_operation(self._dialect, "select", duration, "error")
            logger.error("Failed to decrypt refresh token for user %s: %s", user_id, e)
            return None

    async def get_refresh_token_by_provisioning_client_id(
        self, provisioning_client_id: str
    ) -> dict | None:
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

        # ``assert`` is stripped under ``python -O``, which would silently
        # turn a missing TOKEN_ENCRYPTION_KEY into an ``AttributeError`` on
        # the next ``self.cipher.encrypt(...)``. Raise explicitly instead
        # (PR #758 round-4 review medium 1).
        if self.cipher is None:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY is not set — token storage operations unavailable"
            )

        async with self._db() as db:
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
                "No refresh token found for provisioning_client_id %s...",
                provisioning_client_id[:16],
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
                "Refresh token for provisioning_client_id %s... has expired",
                provisioning_client_id[:16],
            )
            return None

        try:
            decrypted_token = self.cipher.decrypt(encrypted_token).decode()
            scopes = json.loads(scopes_json) if scopes_json else None

            logger.debug(
                "Retrieved refresh token for provisioning_client_id %s... (user_id: %s)",
                provisioning_client_id[:16],
                user_id,
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
                "Failed to decrypt refresh token for provisioning_client_id %s...: %s",
                provisioning_client_id[:16],
                e,
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

        start_time = time.time()
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "DELETE FROM refresh_tokens WHERE user_id = ?",
                    (user_id,),
                )
                await db.commit()
                deleted = cursor.rowcount > 0

            duration = time.time() - start_time
            record_db_operation(self._dialect, "delete", duration, "success")

            if deleted:
                logger.info("Deleted refresh token for user %s", user_id)
                await self._audit_log(
                    event="delete_refresh_token",
                    user_id=user_id,
                    auth_method="offline_access",
                )
            else:
                logger.debug("No refresh token to delete for user %s", user_id)

            return deleted
        except Exception:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "delete", duration, "error")
            raise

    async def get_all_user_ids(self) -> list[str]:
        """
        Get list of all user IDs with stored refresh tokens.

        Returns:
            List of user IDs
        """
        if not self._initialized:
            await self.initialize()

        async with self._db() as db:
            async with db.execute(
                "SELECT user_id FROM refresh_tokens ORDER BY updated_at DESC"
            ) as cursor:
                rows = await cursor.fetchall()

        user_ids = [row[0] for row in rows]
        logger.debug("Found %s users with refresh tokens", len(user_ids))
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

        async with self._db() as db:
            cursor = await db.execute(
                "DELETE FROM refresh_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            await db.commit()
            deleted = cursor.rowcount

        if deleted > 0:
            logger.info("Cleaned up %s expired refresh token(s)", deleted)

        return deleted

    async def store_oauth_client(
        self,
        client_id: str,
        client_secret: str,
        client_id_issued_at: int,
        client_secret_expires_at: int,
        redirect_uris: list[str],
        registration_access_token: str | None = None,
        registration_client_uri: str | None = None,
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

        # ``assert`` is stripped under ``python -O``, which would silently
        # turn a missing TOKEN_ENCRYPTION_KEY into an ``AttributeError`` on
        # the next ``self.cipher.encrypt(...)``. Raise explicitly instead
        # (PR #758 round-4 review medium 1).
        if self.cipher is None:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY is not set — token storage operations unavailable"
            )

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

        async with self._db() as db:
            # Singleton row pinned at id=1; ON CONFLICT preserves the
            # original ``created_at`` because it's omitted from the update.
            await db.execute(
                """
                INSERT INTO oauth_clients
                (id, client_id, encrypted_client_secret, client_id_issued_at,
                 client_secret_expires_at, redirect_uris, encrypted_registration_access_token,
                 registration_client_uri, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    client_id = EXCLUDED.client_id,
                    encrypted_client_secret = EXCLUDED.encrypted_client_secret,
                    client_id_issued_at = EXCLUDED.client_id_issued_at,
                    client_secret_expires_at = EXCLUDED.client_secret_expires_at,
                    redirect_uris = EXCLUDED.redirect_uris,
                    encrypted_registration_access_token = EXCLUDED.encrypted_registration_access_token,
                    registration_client_uri = EXCLUDED.registration_client_uri,
                    updated_at = EXCLUDED.updated_at
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
            "Stored OAuth client credentials (client_id: %s..., expires at %s)",
            client_id[:16],
            client_secret_expires_at,
        )

        # Audit log
        await self._audit_log(
            event="store_oauth_client",
            user_id="system",
            auth_method="oauth",
        )

    async def get_oauth_client(self) -> dict | None:
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

        # ``assert`` is stripped under ``python -O``, which would silently
        # turn a missing TOKEN_ENCRYPTION_KEY into an ``AttributeError`` on
        # the next ``self.cipher.encrypt(...)``. Raise explicitly instead
        # (PR #758 round-4 review medium 1).
        if self.cipher is None:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY is not set — token storage operations unavailable"
            )

        async with self._db() as db:
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
                "OAuth client has expired (expired at %s), deleting", expires_at
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
                "Retrieved OAuth client credentials (client_id: %s...)", client_id[:16]
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
            logger.error("Failed to decrypt OAuth client credentials: %s", e)
            return None

    async def delete_oauth_client(self) -> bool:
        """
        Delete OAuth client credentials.

        Returns:
            True if client was deleted, False if not found
        """
        if not self._initialized:
            await self.initialize()

        async with self._db() as db:
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

        async with self._db() as db:
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
        resource_type: str | None = None,
        resource_id: str | None = None,
        auth_method: str | None = None,
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

        hostname = socket.gethostname()
        timestamp = int(time.time())

        async with self._db() as db:
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
        user_id: str | None = None,
        since: int | None = None,
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

        # Explicit column list (not ``SELECT *``) so future audit_logs
        # schema additions don't silently leak into the dict return.
        query = (
            "SELECT id, timestamp, event, user_id, resource_type, "
            "resource_id, auth_method, hostname FROM audit_logs WHERE 1=1"
        )
        params = []

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        if since:
            query += " AND timestamp >= ?"
            params.append(since)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with self._db() as db:
            # ``query`` is built via string concatenation, but the fragments
            # come only from this function's branches above (no
            # user-controlled SQL); user input flows through ``params``.
            # Bare ``# NOSONAR`` silences taint analysers; defensive.
            async with db.execute(query, params) as cursor:  # NOSONAR
                rows = await cursor.fetchall()

        return [dict(row) for row in rows]

    async def store_oauth_session(
        self,
        session_id: str,
        client_redirect_uri: str,
        state: str | None = None,
        code_challenge: str | None = None,
        code_challenge_method: str | None = None,
        mcp_authorization_code: str | None = None,
        client_id: str | None = None,
        flow_type: str = "hybrid",
        is_provisioning: bool = False,
        requested_scopes: str | None = None,
        nonce: str | None = None,
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
            nonce: OIDC ``nonce`` value bound to this auth request, returned
                in the ID token and verified on callback (PR #758 finding 2).
            ttl_seconds: Session TTL in seconds
        """
        if not self._initialized:
            await self.initialize()

        now = int(time.time())
        expires_at = now + ttl_seconds

        async with self._db() as db:
            await db.execute(
                """
                INSERT INTO oauth_sessions
                (session_id, client_id, client_redirect_uri, state, code_challenge,
                 code_challenge_method, mcp_authorization_code, flow_type,
                 is_provisioning, requested_scopes, nonce, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    nonce,
                    now,
                    expires_at,
                ),
            )
            await db.commit()

        logger.debug(
            "Stored OAuth session %s (expires in %ss)", session_id, ttl_seconds
        )

    async def get_oauth_session(self, session_id: str) -> dict | None:
        """
        Retrieve OAuth session by session ID.

        Returns:
            Session dictionary or None if not found/expired
        """
        if not self._initialized:
            await self.initialize()

        async with self._db() as db:
            async with db.execute(
                "SELECT * FROM oauth_sessions WHERE session_id = ?", (session_id,)
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            return None

        session = dict(row)

        # Check expiration
        if session["expires_at"] < time.time():
            logger.debug("OAuth session %s has expired", session_id)
            await self.delete_oauth_session(session_id)
            return None

        return session

    async def get_oauth_session_by_mcp_code(
        self, mcp_authorization_code: str
    ) -> dict | None:
        """
        Retrieve OAuth session by MCP authorization code.

        Returns:
            Session dictionary or None if not found/expired
        """
        if not self._initialized:
            await self.initialize()

        async with self._db() as db:
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
                "OAuth session with MCP code %s... has expired",
                mcp_authorization_code[:16],
            )
            await self.delete_oauth_session(session["session_id"])
            return None

        return session

    async def update_oauth_session(
        self,
        session_id: str,
        user_id: str | None = None,
        idp_access_token: str | None = None,
        idp_refresh_token: str | None = None,
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

        async with self._db() as db:
            # ``update_fields`` only ever contains hardcoded ``"col = ?"``
            # literals from this function's branches above — there is no
            # user-controlled input in the SQL string itself, only in the
            # ``params`` bound below. Bare ``# NOSONAR`` silences taint
            # analysers that flag f-string SQL construction (e.g.
            # ``python:S2077``); no such rule fires today, defensive.
            cursor = await db.execute(
                f"""
                UPDATE oauth_sessions
                SET {", ".join(update_fields)}
                WHERE session_id = ?
                """,  # NOSONAR
                params,
            )
            await db.commit()
            updated = cursor.rowcount > 0

        if updated:
            logger.debug("Updated OAuth session %s", session_id)

        return updated

    async def delete_oauth_session(self, session_id: str) -> bool:
        """
        Delete OAuth session.

        Returns:
            True if session was deleted, False if not found
        """
        if not self._initialized:
            await self.initialize()

        async with self._db() as db:
            cursor = await db.execute(
                "DELETE FROM oauth_sessions WHERE session_id = ?", (session_id,)
            )
            await db.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            logger.debug("Deleted OAuth session %s", session_id)

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

        async with self._db() as db:
            cursor = await db.execute(
                "DELETE FROM oauth_sessions WHERE expires_at < ?", (now,)
            )
            await db.commit()
            deleted = cursor.rowcount

        if deleted > 0:
            logger.info("Cleaned up %s expired OAuth session(s)", deleted)

        return deleted

    # ============================================================================
    # Browser Sessions (OAuth admin UI)
    # ============================================================================
    #
    # Maps a cryptographically random `session_id` (cookie value) to the
    # authenticated user_id. Replaces the prior `mcp_session=<user_id>`
    # cookie pattern (issue #626 finding 2). Cookie value is opaque, expires,
    # and can be revoked server-side without forcing the user to roll their
    # IdP `sub`.

    async def create_browser_session(
        self,
        session_id: str,
        user_id: str,
        ttl_seconds: int = 86400 * 30,
    ) -> None:
        """Persist a random session_id → user_id mapping for browser auth."""
        if not self._initialized:
            await self.initialize()

        now = int(time.time())
        expires_at = now + ttl_seconds

        async with self._db() as db:
            await db.execute(
                """
                INSERT INTO browser_sessions
                (session_id, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (session_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    created_at = EXCLUDED.created_at,
                    expires_at = EXCLUDED.expires_at
                """,
                (session_id, user_id, now, expires_at),
            )
            await db.commit()

        logger.debug(
            "Stored browser session %s for user %s (expires in %ss)",
            session_id[:8],
            user_id,
            ttl_seconds,
        )

        # Audit log to match the pattern used by the other security-relevant
        # storage operations (PR #758 round-3 nit 5). Browser session
        # establishment is a security-relevant event.
        await self._audit_log(
            event="create_browser_session",
            user_id=user_id,
            resource_type="browser_session",
            resource_id=session_id[:8],
        )

    async def get_browser_session_user(self, session_id: str) -> str | None:
        """Look up the user_id bound to a browser session_id, or None.

        Returns None when the session is unknown or expired. Expired rows
        are deleted on encounter to keep the table small.
        """
        if not self._initialized:
            await self.initialize()

        async with self._db() as db:
            async with db.execute(
                "SELECT user_id, expires_at FROM browser_sessions WHERE session_id = ?",
                (session_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            return None

        if row["expires_at"] < time.time():
            logger.debug("Browser session %s expired", session_id[:8])
            await self.delete_browser_session(session_id)
            return None

        return row["user_id"]

    async def delete_browser_session(self, session_id: str) -> bool:
        """Delete a browser session row. Returns True when a row was removed."""
        if not self._initialized:
            await self.initialize()

        # DELETE ... RETURNING (SQLite ≥ 3.35) reads ``user_id`` atomically
        # with the delete itself, so the audit log can't race against a
        # concurrent delete that empties the row between SELECT and DELETE
        # (PR #758 round-3 review).
        user_id: str | None = None
        async with self._db() as db:
            async with db.execute(
                "DELETE FROM browser_sessions WHERE session_id = ? RETURNING user_id",
                (session_id,),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()

        deleted = row is not None
        if deleted:
            user_id = row[0]
            logger.debug("Deleted browser session %s", session_id[:8])
            if user_id:
                await self._audit_log(
                    event="delete_browser_session",
                    user_id=user_id,
                    resource_type="browser_session",
                    resource_id=session_id[:8],
                )
        return deleted

    async def cleanup_expired_browser_sessions(self) -> int:
        """Remove expired ``browser_sessions`` rows.

        Returns the number of rows deleted. Called by the periodic cleanup
        task in ``app.py``. Without this users who never explicitly log out
        leave session rows behind that only get deleted lazily on lookup
        (PR #758 finding 6).
        """
        if not self._initialized:
            await self.initialize()

        now = int(time.time())

        async with self._db() as db:
            cursor = await db.execute(
                "DELETE FROM browser_sessions WHERE expires_at < ?", (now,)
            )
            await db.commit()
            deleted = cursor.rowcount

        if deleted > 0:
            logger.info("Cleaned up %s expired browser session(s)", deleted)

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

        async with self._db() as db:
            await db.execute(
                """
                INSERT INTO registered_webhooks (webhook_id, preset_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT (webhook_id) DO UPDATE SET
                    preset_id = EXCLUDED.preset_id,
                    created_at = EXCLUDED.created_at
                """,
                (webhook_id, preset_id, int(time.time())),
            )
            await db.commit()

        logger.debug("Stored webhook %s for preset '%s'", webhook_id, preset_id)

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

        async with self._db() as db:
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

        async with self._db() as db:
            cursor = await db.execute(
                "DELETE FROM registered_webhooks WHERE webhook_id = ?", (webhook_id,)
            )
            await db.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            logger.debug("Deleted webhook %s from tracking", webhook_id)

        return deleted

    async def list_all_webhooks(self) -> list[dict]:
        """
        List all tracked webhooks with metadata.

        Returns:
            List of webhook dictionaries with keys: webhook_id, preset_id, created_at
        """
        if not self._initialized:
            await self.initialize()

        async with self._db() as db:
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

        async with self._db() as db:
            cursor = await db.execute(
                "DELETE FROM registered_webhooks WHERE preset_id = ?", (preset_id,)
            )
            await db.commit()
            deleted = cursor.rowcount

        if deleted > 0:
            logger.debug("Cleared %s webhook(s) for preset '%s'", deleted, preset_id)

        return deleted

    # ============================================================================
    # App Password Storage (multi-user BasicAuth mode)
    # ============================================================================

    async def store_app_password(
        self,
        user_id: str,
        app_password: str,
    ) -> None:
        """
        Store encrypted app password for background sync (multi-user BasicAuth mode).

        Args:
            user_id: Nextcloud user ID
            app_password: Nextcloud app password to store
        """
        if not self._initialized:
            await self.initialize()

        if not self.cipher:
            raise RuntimeError(
                "Encryption key not configured. "
                "Set TOKEN_ENCRYPTION_KEY for app password storage."
            )

        encrypted_password = self.cipher.encrypt(app_password.encode())
        now = int(time.time())

        start_time = time.time()
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    INSERT INTO app_passwords
                    (user_id, encrypted_password, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (user_id) DO UPDATE SET
                        encrypted_password = EXCLUDED.encrypted_password,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (user_id, encrypted_password, now, now),
                )
                await db.commit()

            duration = time.time() - start_time
            record_db_operation(self._dialect, "insert", duration, "success")
            logger.info("Stored app password for user %s", user_id)

        except Exception:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "insert", duration, "error")
            raise

        # Audit log
        await self._audit_log(
            event="store_app_password",
            user_id=user_id,
            auth_method="app_password",
        )

    async def get_app_password(self, user_id: str) -> str | None:
        """
        Retrieve and decrypt app password for a user.

        Args:
            user_id: Nextcloud user ID

        Returns:
            Decrypted app password, or None if not found
        """
        if not self._initialized:
            await self.initialize()

        if not self.cipher:
            raise RuntimeError(
                "Encryption key not configured. "
                "Set TOKEN_ENCRYPTION_KEY for app password retrieval."
            )

        start_time = time.time()
        try:
            async with self._db() as db:
                async with db.execute(
                    "SELECT encrypted_password FROM app_passwords WHERE user_id = ?",
                    (user_id,),
                ) as cursor:
                    row = await cursor.fetchone()

            if not row:
                logger.debug("No app password found for user %s", user_id)
                duration = time.time() - start_time
                record_db_operation(self._dialect, "select", duration, "success")
                return None

            encrypted_password = row[0]
            decrypted_password = self.cipher.decrypt(encrypted_password).decode()

            duration = time.time() - start_time
            record_db_operation(self._dialect, "select", duration, "success")
            logger.debug("Retrieved app password for user %s", user_id)

            return decrypted_password

        except Exception as e:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "select", duration, "error")
            logger.error("Failed to decrypt app password for user %s: %s", user_id, e)
            return None

    async def delete_app_password(self, user_id: str) -> bool:
        """
        Delete app password for a user.

        Args:
            user_id: Nextcloud user ID

        Returns:
            True if password was deleted, False if not found
        """
        if not self._initialized:
            await self.initialize()

        start_time = time.time()
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "DELETE FROM app_passwords WHERE user_id = ?",
                    (user_id,),
                )
                await db.commit()
                deleted = cursor.rowcount > 0

            duration = time.time() - start_time
            record_db_operation(self._dialect, "delete", duration, "success")

            if deleted:
                logger.info("Deleted app password for user %s", user_id)
                await self._audit_log(
                    event="delete_app_password",
                    user_id=user_id,
                    auth_method="app_password",
                )
            else:
                logger.debug("No app password to delete for user %s", user_id)

            return deleted

        except Exception:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "delete", duration, "error")
            raise

    async def get_all_app_password_user_ids(self) -> list[str]:
        """
        Get list of all user IDs with stored app passwords.

        Returns:
            List of user IDs
        """
        if not self._initialized:
            await self.initialize()

        async with self._db() as db:
            async with db.execute(
                "SELECT user_id FROM app_passwords ORDER BY updated_at DESC"
            ) as cursor:
                rows = await cursor.fetchall()

        user_ids = [row[0] for row in rows]
        logger.debug("Found %s users with app passwords", len(user_ids))
        return user_ids

    async def cleanup_invalid_app_passwords(self, nextcloud_host: str) -> list[str]:
        """
        Validate stored app passwords against Nextcloud and remove invalid ones.

        Makes a lightweight OCS request for each stored user to check if credentials
        are still valid. Removes entries that return 401/403.

        Args:
            nextcloud_host: Nextcloud base URL

        Returns:
            List of user IDs whose app passwords were removed
        """
        if not self._initialized:
            await self.initialize()

        user_ids = await self.get_all_app_password_user_ids()
        if not user_ids:
            return []

        removed: list[str] = []

        async def _validate_user(user_id: str) -> None:
            try:
                app_data = await self.get_app_password_with_scopes(user_id)
                if not app_data:
                    return

                app_password = app_data["app_password"]
                # Authenticate as the stored loginName, not the UID: Nextcloud
                # keys app-password auth on the loginName, which differs from
                # the UID for OIDC-provisioned users. Using the UID here would
                # 401 a *valid* password and wrongly delete it. Falls back to
                # the UID for legacy rows without a stored loginName.
                login_name = app_data.get("username") or user_id

                async with httpx.AsyncClient(
                    base_url=nextcloud_host,
                    auth=httpx.BasicAuth(login_name, app_password),
                    timeout=10.0,
                ) as client:
                    response = await client.get(
                        "/ocs/v2.php/cloud/user",
                        headers={
                            "OCS-APIRequest": "true",
                            "Accept": "application/json",
                        },
                    )

                if response.status_code in (401, 403):
                    logger.info(
                        "App password for %s is invalid (HTTP %s), removing",
                        user_id,
                        response.status_code,
                    )
                    await self.delete_app_password(user_id)
                    removed.append(user_id)
                else:
                    logger.debug(
                        "App password for %s validated (HTTP %s)",
                        user_id,
                        response.status_code,
                    )

            except Exception as e:
                logger.warning("Could not validate app password for %s: %s", user_id, e)

        async with anyio.create_task_group() as tg:
            for user_id in user_ids:
                tg.start_soon(_validate_user, user_id)

        return removed

    # ── Login Flow v2: Scoped App Passwords ──────────────────────────────

    async def store_app_password_with_scopes(
        self,
        user_id: str,
        app_password: str,
        scopes: list[str] | None = None,
        username: str | None = None,
    ) -> None:
        """Store encrypted app password with optional scopes and Nextcloud username.

        Args:
            user_id: MCP user ID (identity from OAuth token or session)
            app_password: Nextcloud app password to encrypt and store
            scopes: List of granted scopes (None = all scopes allowed)
            username: Nextcloud loginName from Login Flow v2 response

        Raises:
            ValueError: If any scope is not in ALL_SUPPORTED_SCOPES
        """
        if not self._initialized:
            await self.initialize()

        if not self.cipher:
            raise RuntimeError(
                "Encryption key not configured. "
                "Set TOKEN_ENCRYPTION_KEY for app password storage."
            )

        # Defense-in-depth: validate scopes at storage layer
        if scopes is not None:
            from nextcloud_mcp_server.models.auth import (  # noqa: PLC0415
                ALL_SUPPORTED_SCOPES,
            )

            invalid = [s for s in scopes if s not in ALL_SUPPORTED_SCOPES]
            if invalid:
                raise ValueError(f"Invalid scopes: {invalid}")

        encrypted_password = self.cipher.encrypt(app_password.encode())
        scopes_json = json.dumps(scopes) if scopes is not None else None
        now = int(time.time())

        start_time = time.time()
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    INSERT INTO app_passwords
                    (user_id, encrypted_password, created_at, updated_at, scopes, username)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id) DO UPDATE SET
                        encrypted_password = EXCLUDED.encrypted_password,
                        updated_at = EXCLUDED.updated_at,
                        scopes = EXCLUDED.scopes,
                        username = EXCLUDED.username
                    """,
                    (
                        user_id,
                        encrypted_password,
                        now,
                        now,
                        scopes_json,
                        username,
                    ),
                )
                await db.commit()

            duration = time.time() - start_time
            record_db_operation(self._dialect, "insert", duration, "success")
            logger.info(
                "Stored scoped app password for user %s (scopes=%s, username=%s)",
                user_id,
                "all" if scopes is None else len(scopes),
                username or "N/A",
            )

        except Exception:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "insert", duration, "error")
            raise

        await self._audit_log(
            event="store_app_password_with_scopes",
            user_id=user_id,
            auth_method="app_password",
        )

    async def get_app_password_with_scopes(self, user_id: str) -> dict[str, Any] | None:
        """Retrieve app password with scopes and metadata.

        Args:
            user_id: MCP user ID

        Returns:
            Dict with keys: app_password, scopes, username, created_at, updated_at
            or None if not found
        """
        if not self._initialized:
            await self.initialize()

        if not self.cipher:
            raise RuntimeError(
                "Encryption key not configured. "
                "Set TOKEN_ENCRYPTION_KEY for app password retrieval."
            )

        start_time = time.time()
        try:
            async with self._db() as db:
                async with db.execute(
                    """
                    SELECT encrypted_password, scopes, username, created_at, updated_at
                    FROM app_passwords WHERE user_id = ?
                    """,
                    (user_id,),
                ) as cursor:
                    row = await cursor.fetchone()

            if not row:
                logger.debug("No app password found for user %s", user_id)
                duration = time.time() - start_time
                record_db_operation(self._dialect, "select", duration, "success")
                return None

            encrypted_password, scopes_json, username, created_at, updated_at = row
            decrypted_password = self.cipher.decrypt(encrypted_password).decode()
            scopes = json.loads(scopes_json) if scopes_json else None

            duration = time.time() - start_time
            record_db_operation(self._dialect, "select", duration, "success")

            return {
                "app_password": decrypted_password,
                "scopes": scopes,
                "username": username,
                "created_at": created_at,
                "updated_at": updated_at,
            }

        except Exception:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "select", duration, "error")
            raise

    async def update_app_password_scopes(self, user_id: str, scopes: list[str]) -> bool:
        """Update only the scopes for an existing app password (no decrypt/re-encrypt).

        Args:
            user_id: MCP user ID
            scopes: New scope list

        Returns:
            True if a row was updated, False if user not found
        """
        if not self._initialized:
            await self.initialize()

        scopes_json = json.dumps(scopes)
        now = int(time.time())
        start_time = time.time()
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "UPDATE app_passwords SET scopes = ?, updated_at = ? WHERE user_id = ?",
                    (scopes_json, now, user_id),
                )
                await db.commit()
                updated = cursor.rowcount > 0

            duration = time.time() - start_time
            record_db_operation(self._dialect, "update", duration, "success")

            if updated:
                await self._audit_log(
                    event="update_app_password_scopes",
                    user_id=user_id,
                    auth_method="app_password",
                )

            return updated

        except Exception:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "update", duration, "error")
            raise

    # ── Login Flow v2: Session Tracking ──────────────────────────────────

    async def store_login_flow_session(
        self,
        user_id: str,
        poll_token: str,
        poll_endpoint: str,
        requested_scopes: list[str] | None = None,
        expires_at: int | None = None,
    ) -> None:
        """Store a Login Flow v2 polling session.

        Args:
            user_id: MCP user ID
            poll_token: Token for polling (will be encrypted)
            poll_endpoint: URL to poll for completion
            requested_scopes: Scopes requested in this flow
            expires_at: Expiration timestamp (defaults to 20 minutes from now)
        """
        if not self._initialized:
            await self.initialize()

        if not self.cipher:
            raise RuntimeError(
                "Encryption key not configured. "
                "Set TOKEN_ENCRYPTION_KEY for login flow session storage."
            )

        encrypted_token = self.cipher.encrypt(poll_token.encode())
        scopes_json = json.dumps(requested_scopes) if requested_scopes else None
        now = int(time.time())
        if expires_at is None:
            expires_at = now + 1200  # 20 minutes default

        start_time = time.time()
        try:
            async with self._db() as db:
                await db.execute(
                    """
                    INSERT INTO login_flow_sessions
                    (user_id, encrypted_poll_token, poll_endpoint, requested_scopes,
                     created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id) DO UPDATE SET
                        encrypted_poll_token = EXCLUDED.encrypted_poll_token,
                        poll_endpoint = EXCLUDED.poll_endpoint,
                        requested_scopes = EXCLUDED.requested_scopes,
                        created_at = EXCLUDED.created_at,
                        expires_at = EXCLUDED.expires_at
                    """,
                    (
                        user_id,
                        encrypted_token,
                        poll_endpoint,
                        scopes_json,
                        now,
                        expires_at,
                    ),
                )
                await db.commit()

            duration = time.time() - start_time
            record_db_operation(self._dialect, "insert", duration, "success")
            logger.info("Stored login flow session for user %s", user_id)

        except Exception:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "insert", duration, "error")
            raise

    async def get_login_flow_session(self, user_id: str) -> dict[str, Any] | None:
        """Retrieve a pending Login Flow v2 session.

        Returns None if session doesn't exist or has expired.

        Args:
            user_id: MCP user ID

        Returns:
            Dict with keys: poll_token, poll_endpoint, requested_scopes, created_at, expires_at
            or None if not found/expired
        """
        if not self._initialized:
            await self.initialize()

        if not self.cipher:
            raise RuntimeError(
                "Encryption key not configured. "
                "Set TOKEN_ENCRYPTION_KEY for login flow session retrieval."
            )

        now = int(time.time())
        start_time = time.time()
        try:
            async with self._db() as db:
                async with db.execute(
                    """
                    SELECT encrypted_poll_token, poll_endpoint, requested_scopes,
                           created_at, expires_at
                    FROM login_flow_sessions
                    WHERE user_id = ? AND expires_at > ?
                    """,
                    (user_id, now),
                ) as cursor:
                    row = await cursor.fetchone()

            if not row:
                duration = time.time() - start_time
                record_db_operation(self._dialect, "select", duration, "success")
                return None

            encrypted_token, poll_endpoint, scopes_json, created_at, expires_at = row
            poll_token = self.cipher.decrypt(encrypted_token).decode()
            requested_scopes = json.loads(scopes_json) if scopes_json else None

            duration = time.time() - start_time
            record_db_operation(self._dialect, "select", duration, "success")

            return {
                "poll_token": poll_token,
                "poll_endpoint": poll_endpoint,
                "requested_scopes": requested_scopes,
                "created_at": created_at,
                "expires_at": expires_at,
            }

        except Exception as e:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "select", duration, "error")
            logger.error(
                "Failed to retrieve login flow session for user %s: %s", user_id, e
            )
            raise

    async def delete_login_flow_session(self, user_id: str) -> bool:
        """Delete a Login Flow v2 session.

        Args:
            user_id: MCP user ID

        Returns:
            True if session was deleted, False if not found
        """
        if not self._initialized:
            await self.initialize()

        start_time = time.time()
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "DELETE FROM login_flow_sessions WHERE user_id = ?",
                    (user_id,),
                )
                await db.commit()
                deleted = cursor.rowcount > 0

            duration = time.time() - start_time
            record_db_operation(self._dialect, "delete", duration, "success")

            if deleted:
                logger.info("Deleted login flow session for user %s", user_id)
                await self._audit_log(
                    event="delete_login_flow_session",
                    user_id=user_id,
                    auth_method="login_flow",
                )

            return deleted

        except Exception:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "delete", duration, "error")
            raise

    async def delete_expired_login_flow_sessions(self) -> int:
        """Delete all expired Login Flow v2 sessions.

        Returns:
            Number of sessions deleted
        """
        if not self._initialized:
            await self.initialize()

        now = int(time.time())
        start_time = time.time()
        try:
            async with self._db() as db:
                cursor = await db.execute(
                    "DELETE FROM login_flow_sessions WHERE expires_at <= ?",
                    (now,),
                )
                await db.commit()
                count = cursor.rowcount

            duration = time.time() - start_time
            record_db_operation(self._dialect, "delete", duration, "success")

            if count > 0:
                logger.info("Cleaned up %s expired login flow sessions", count)
                await self._audit_log(
                    event="delete_expired_login_flow_sessions",
                    user_id="system",
                    auth_method="login_flow",
                )

            return count

        except Exception:
            duration = time.time() - start_time
            record_db_operation(self._dialect, "delete", duration, "error")
            raise


_shared_instance: RefreshTokenStorage | None = None
_shared_lock: anyio.Lock = anyio.Lock()


async def get_shared_storage() -> RefreshTokenStorage:
    """Get the process-wide RefreshTokenStorage singleton (lock-protected).

    All modules that need storage should use this function instead of
    creating their own lazy singletons. The lock ensures thread-safe
    initialization on concurrent first-access.
    """
    global _shared_instance
    async with _shared_lock:
        if _shared_instance is None:
            _shared_instance = RefreshTokenStorage.from_env()
            await _shared_instance.initialize()
    return _shared_instance


async def generate_encryption_key() -> str:
    """
    Generate a new Fernet encryption key.

    Returns:
        Base64-encoded encryption key suitable for TOKEN_ENCRYPTION_KEY env var
    """
    return Fernet.generate_key().decode()


# Example usage
if __name__ == "__main__":
    import anyio

    async def main():
        # Generate a key for testing
        key = await generate_encryption_key()
        print(f"Generated encryption key: {key}")
        print(f"Set this in your environment: export TOKEN_ENCRYPTION_KEY='{key}'")

    anyio.run(main)
