"""Stateful (RuleBasedStateMachine) tests for ordering-sensitive stores.

Each machine keeps a plain-dict model next to the real store, applies random
sequences of the public operations, and checks that the store agrees with the
model after every step. The point is to catch bugs that only show up in a
particular order: re-login after expiry, delete of an expired row, a stale-row
purge racing a resubmit, an early-refresh window crossed by a clock advance.

How these are wired:

- Hypothesis drives rules synchronously, so every async store call goes
  through one ``anyio.from_thread`` blocking portal per test (one event loop
  for the whole run, so the SQLite engine stays on one loop).
- The machine is built inside a plain test function and run with
  ``run_state_machine_as_test``, so pytest fixtures (the portal, a temp SQLite
  storage) are available. The storage is created once per test and its table
  is wiped in each machine's ``__init__``: ``initialize()`` runs Alembic and
  costs ~230 ms, a ``DELETE`` costs ~4 ms.
- Time is an explicit integer clock the machine advances with a rule. The
  store's module-level time source is monkeypatched to read it; nothing sleeps.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anyio.from_thread import BlockingPortal, start_blocking_portal
from cryptography.fernet import Fernet
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from nextcloud_mcp_server.auth import storage as storage_module
from nextcloud_mcp_server.auth import token_broker
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.auth.token_broker import TokenCache
from nextcloud_mcp_server.vector.batch_ocr_store import BatchOcrJobStore

pytestmark = pytest.mark.unit

# Budgets keep each machine at ~1 s locally. A SQLite call costs ~4 ms (NullPool
# opens a connection per acquire) and a portal hop ~0.5 ms, so the DB machines
# get fewer steps. Raise these locally when hunting a bug, not here.
_MEMORY_SETTINGS = settings(max_examples=25, stateful_step_count=20, deadline=None)
_DB_SETTINGS = settings(max_examples=40, stateful_step_count=12, deadline=None)


class Clock:
    """Integer seconds, advanced only by a rule."""

    def __init__(self, start: int = 1_000_000) -> None:
        self.now = start

    def time(self) -> float:
        return float(self.now)


@pytest.fixture
def portal() -> Iterator[BlockingPortal]:
    with start_blocking_portal() as p:
        yield p


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """A clock that both stores under test read instead of the wall clock."""
    c = Clock()
    # storage.py calls ``time.time()``; replace its module reference only.
    monkeypatch.setattr(storage_module, "time", SimpleNamespace(time=c.time))

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> FakeDatetime:  # type: ignore[override]
            return cls.fromtimestamp(c.now, tz or timezone.utc)

    monkeypatch.setattr(token_broker, "datetime", FakeDatetime)
    return c


@pytest.fixture
def sqlite_storage(portal: BlockingPortal) -> Iterator[RefreshTokenStorage]:
    with tempfile.TemporaryDirectory() as tmp:
        storage = RefreshTokenStorage(
            db_path=str(Path(tmp) / "machine.db"),
            encryption_key=Fernet.generate_key(),
        )
        portal.call(storage.initialize)
        yield storage
        portal.call(storage.close)


def _wipe(portal: BlockingPortal, storage: RefreshTokenStorage, table: str) -> None:
    async def run() -> None:
        async with storage.acquire() as db:
            await db.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
            await db.commit()

    portal.call(run)


# ---------------------------------------------------------------------------
# TokenCache: TTL, early-refresh window, lazy deletion on read
# ---------------------------------------------------------------------------

_USERS = st.sampled_from(["alice", "bob"])
# Small TTL and clock steps, so sequences land exactly on the expiry and
# early-refresh boundaries where off-by-one bugs live.
_TTL = 10
_EARLY = 3


def test_token_cache_state_machine(portal: BlockingPortal, clock: Clock) -> None:
    class TokenCacheMachine(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            clock.now = 1_000_000
            self.cache = TokenCache(ttl_seconds=_TTL, early_refresh_seconds=_EARLY)
            self.model: dict[str, tuple[str, int]] = {}  # user -> (token, expiry)
            self.serial = 0

        @rule(
            user=_USERS,
            # 0 is falsy, so set() treats it like None (default TTL); keep it in.
            expires_in=st.none() | st.integers(min_value=0, max_value=_TTL + 2),
        )
        def set(self, user: str, expires_in: int | None) -> None:
            self.serial += 1
            token = f"tok-{self.serial}"
            portal.call(self.cache.set, user, token, expires_in)
            self.model[user] = (token, clock.now + (expires_in or _TTL))

        @rule(user=_USERS)
        def invalidate(self, user: str) -> None:
            portal.call(self.cache.invalidate, user)
            self.model.pop(user, None)

        @rule(seconds=st.integers(min_value=0, max_value=_TTL // 2 + 1))
        def advance(self, seconds: int) -> None:
            clock.now += seconds

        def _expected(self, user: str) -> str | None:
            if user not in self.model:
                return None
            token, expiry = self.model[user]
            if clock.now >= expiry:
                del self.model[user]  # get() drops an expired entry
                return None
            if clock.now >= expiry - _EARLY:
                return None  # inside the early-refresh window: kept, not served
            return token

        @invariant()
        def get_matches_model(self) -> None:
            for user in ("alice", "bob"):
                assert portal.call(self.cache.get, user) == self._expected(user), user

        @invariant()
        def entries_match_model(self) -> None:
            # Lazy deletion: an expired entry lingers until it is read, so the
            # key set must track the model's (which mirrors that) exactly.
            assert set(self.cache._cache) == set(self.model)

    run_state_machine_as_test(TokenCacheMachine, settings=_MEMORY_SETTINGS)


# ---------------------------------------------------------------------------
# RefreshTokenStorage: refresh-token rows + cached profile, with expiry
# ---------------------------------------------------------------------------


def test_refresh_token_storage_state_machine(
    portal: BlockingPortal, clock: Clock, sqlite_storage: RefreshTokenStorage
) -> None:
    storage = sqlite_storage
    # Lifetimes within a tick or two of now, so reads hit ``expires_at == now``.
    ttls = st.none() | st.integers(min_value=-1, max_value=2)
    cipher = storage.cipher
    assert cipher is not None
    decrypt = cipher.decrypt  # a bound method keeps the narrowing in the closure

    async def all_rows() -> dict[str, tuple[Any, ...]]:
        async with storage.acquire() as db:
            async with db.execute(
                "SELECT user_id, encrypted_token, expires_at, flow_type, "
                "provisioned_at, scopes, updated_at, user_profile, profile_cached_at "
                "FROM refresh_tokens"
            ) as cursor:
                rows = await cursor.fetchall()
        return {
            r[0]: (
                decrypt(r[1]).decode(),
                r[2],
                r[3],
                r[4],
                json.loads(r[5]) if r[5] else None,
                r[6],
                {**json.loads(r[7]), "_cached_at": r[8]} if r[7] else None,
            )
            for r in rows
        }

    class RefreshTokenMachine(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            clock.now = 1_000_000
            _wipe(portal, storage, "refresh_tokens")
            # user -> row as the store should hold it (incl. expired-but-unread)
            self.rows: dict[str, dict[str, Any]] = {}
            self.serial = 0

        # Hypothesis favours short runs, and most runs never got as far as a
        # logged-in user with a profile. Start every run from one, so re-login,
        # expiry and cleanup act on a populated row from the first step.
        @initialize(ttl=ttls)
        def logged_in_with_profile(self, ttl: int | None) -> None:
            self.store("alice", ttl, "hybrid", None)
            self.store_profile("alice", "A")

        @rule(
            user=_USERS,
            ttl=ttls,
            flow_type=st.sampled_from(["hybrid", "flow2"]),
            scopes=st.none() | st.lists(st.sampled_from(["read", "write"]), max_size=2),
        )
        def store(
            self,
            user: str,
            ttl: int | None,
            flow_type: str,
            scopes: list[str] | None,
        ) -> None:
            self.serial += 1
            token = f"rt-{self.serial}"
            expires_at = None if ttl is None else clock.now + ttl
            portal.call(
                lambda: storage.store_refresh_token(
                    user,
                    token,
                    expires_at=expires_at,
                    flow_type=flow_type,
                    scopes=scopes,
                )
            )
            old = self.rows.get(user, {})
            self.rows[user] = {
                "refresh_token": token,
                "expires_at": expires_at,
                "flow_type": flow_type,
                "token_audience": "nextcloud",
                "provisioned_at": clock.now if flow_type == "flow2" else None,
                "provisioning_client_id": None,
                "scopes": scopes or None,  # [] is stored as NULL
                "updated_at": clock.now,
                # Re-login upserts the row and must keep the cached profile.
                "profile": old.get("profile"),
            }

        @rule(user=_USERS)
        def get(self, user: str) -> None:
            row = self.rows.get(user)
            # The profile read ignores expiry, so check it before get() can delete.
            profile = portal.call(storage.get_user_profile, user)
            assert profile == (row["profile"] if row else None)
            got = portal.call(storage.get_refresh_token, user)
            if row is None:
                assert got is None
                return
            if row["expires_at"] is not None and row["expires_at"] < clock.now:
                assert got is None
                del self.rows[user]  # get() deletes an expired row
                return
            expected = {
                k: v for k, v in row.items() if k not in ("updated_at", "profile")
            }
            assert got == expected

        @rule(user=_USERS)
        def delete(self, user: str) -> None:
            deleted = portal.call(storage.delete_refresh_token, user)
            assert deleted == (user in self.rows)
            self.rows.pop(user, None)

        @rule()
        def cleanup(self) -> None:
            expired = {
                u
                for u, r in self.rows.items()
                if r["expires_at"] is not None and r["expires_at"] < clock.now
            }
            assert portal.call(storage.cleanup_expired_tokens) == len(expired)
            for u in expired:
                del self.rows[u]

        @rule(user=_USERS, name=st.sampled_from(["A", "B"]))
        def store_profile(self, user: str, name: str) -> None:
            portal.call(storage.store_user_profile, user, {"name": name})
            if user in self.rows:  # an UPDATE: no row, no profile
                self.rows[user]["profile"] = {"name": name, "_cached_at": clock.now}

        @rule()
        def tick(self) -> None:
            # One second at a time, so reads land on ``expires_at == now``.
            clock.now += 1

        @rule()
        def get_all_user_ids(self) -> None:
            ids = portal.call(storage.get_all_user_ids)
            assert set(ids) == set(self.rows)
            # ORDER BY updated_at DESC; ties (same clock second) may come in any order.
            stamps = [self.rows[u]["updated_at"] for u in ids]
            assert stamps == sorted(stamps, reverse=True)

        @invariant()
        def table_matches_model(self) -> None:
            # One query per step: the whole table, decrypted, against the model.
            expected = {
                u: (
                    r["refresh_token"],
                    r["expires_at"],
                    r["flow_type"],
                    r["provisioned_at"],
                    r["scopes"],
                    r["updated_at"],
                    r["profile"],
                )
                for u, r in self.rows.items()
            }
            assert portal.call(all_rows) == expected

    run_state_machine_as_test(RefreshTokenMachine, settings=_DB_SETTINGS)


# ---------------------------------------------------------------------------
# BatchOcrJobStore: first-submit-wins insert, delete, stale-etag purge
# ---------------------------------------------------------------------------

_DOC_KEYS = st.fixed_dictionaries(
    {
        "user_id": st.sampled_from(["u1", "u2"]),
        "doc_id": st.just("d1"),
        "doc_type": st.sampled_from(["file", "note"]),
    }
)
_ETAGS = st.sampled_from(["v1", "v2", "v3"])

_Key = tuple[str, str, str, str]  # (user_id, doc_id, doc_type, etag)


def test_batch_ocr_store_state_machine(
    portal: BlockingPortal, sqlite_storage: RefreshTokenStorage
) -> None:
    store = BatchOcrJobStore(sqlite_storage)

    async def all_rows() -> dict[_Key, tuple[str, int]]:
        async with sqlite_storage.acquire() as db:
            async with db.execute(
                "SELECT user_id, doc_id, doc_type, etag, job_id, submitted_at "
                "FROM batch_ocr_jobs"
            ) as cursor:
                rows = await cursor.fetchall()
        return {(r[0], r[1], r[2], r[3]): (r[4], int(r[5])) for r in rows}

    class BatchOcrMachine(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            _wipe(portal, sqlite_storage, "batch_ocr_jobs")
            self.model: dict[_Key, tuple[str, int]] = {}
            self.serial = 0

        @rule(doc=_DOC_KEYS, etag=_ETAGS)
        def insert_pending(self, doc: dict[str, str], etag: str) -> None:
            self.serial += 1
            job_id, submitted_at = f"job-{self.serial}", self.serial
            portal.call(
                lambda: store.insert_pending(
                    **doc, etag=etag, job_id=job_id, submitted_at=submitted_at
                )
            )
            key = (doc["user_id"], doc["doc_id"], doc["doc_type"], etag)
            self.model.setdefault(key, (job_id, submitted_at))  # first one wins

        @rule(doc=_DOC_KEYS, etag=_ETAGS)
        def get(self, doc: dict[str, str], etag: str) -> None:
            got = portal.call(lambda: store.get(**doc, etag=etag))
            expected = self.model.get(
                (doc["user_id"], doc["doc_id"], doc["doc_type"], etag)
            )
            assert ((got.job_id, got.submitted_at) if got else None) == expected

        @rule(doc=_DOC_KEYS, etag=_ETAGS)
        def delete(self, doc: dict[str, str], etag: str) -> None:
            portal.call(lambda: store.delete(**doc, etag=etag))
            self.model.pop((doc["user_id"], doc["doc_id"], doc["doc_type"], etag), None)

        @rule(doc=_DOC_KEYS, keep_etag=_ETAGS)
        def delete_stale_for_doc(self, doc: dict[str, str], keep_etag: str) -> None:
            portal.call(lambda: store.delete_stale_for_doc(**doc, keep_etag=keep_etag))
            prefix = (doc["user_id"], doc["doc_id"], doc["doc_type"])
            for key in [k for k in self.model if k[:3] == prefix and k[3] != keep_etag]:
                del self.model[key]

        @invariant()
        def table_matches_model(self) -> None:
            assert portal.call(all_rows) == self.model

    run_state_machine_as_test(BatchOcrMachine, settings=_DB_SETTINGS)
