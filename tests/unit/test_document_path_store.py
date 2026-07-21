"""Unit tests for the per-user document-path store (ADR-033 Phase 2, Deck #737).

Runs against a real temp-SQLite ``RefreshTokenStorage`` (its ``initialize()``
applies the migrations, incl. ``document_paths`` / revision 009), so this also
exercises the migration and the ON CONFLICT upsert SQL on real SQLite.
"""

import tempfile
from pathlib import Path

import pytest

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.vector.document_path_store import DocumentPathStore

pytestmark = pytest.mark.unit


@pytest.fixture
async def store():
    with tempfile.TemporaryDirectory() as tmp:
        storage = RefreshTokenStorage(db_path=str(Path(tmp) / "paths.db"))
        await storage.initialize()
        yield DocumentPathStore(storage)


async def test_get_missing_returns_empty(store):
    assert await store.get_paths_for_user("alice", "file", ["1"]) == {}


async def test_empty_docs_short_circuits(store):
    assert await store.get_paths_for_user("alice", "file", []) == {}


async def test_upsert_then_get(store):
    await store.upsert(
        user_id="alice", doc_id="1", doc_type="file", file_path="/alice/doc.pdf"
    )
    paths = await store.get_paths_for_user("alice", "file", ["1"])
    assert paths == {"1": "/alice/doc.pdf"}


async def test_upsert_overwrites_in_place(store):
    await store.upsert(
        user_id="alice", doc_id="1", doc_type="file", file_path="/alice/old.pdf"
    )
    await store.upsert(
        user_id="alice", doc_id="1", doc_type="file", file_path="/alice/new.pdf"
    )
    paths = await store.get_paths_for_user("alice", "file", ["1"])
    assert paths == {"1": "/alice/new.pdf"}


async def test_paths_are_per_user(store):
    # The same shared file (doc_id 1) at a different mount path per user.
    await store.upsert(
        user_id="alice", doc_id="1", doc_type="file", file_path="/alice/doc.pdf"
    )
    await store.upsert(
        user_id="bob", doc_id="1", doc_type="file", file_path="/bob/alice/doc.pdf"
    )
    assert await store.get_paths_for_user("alice", "file", ["1"]) == {
        "1": "/alice/doc.pdf"
    }
    assert await store.get_paths_for_user("bob", "file", ["1"]) == {
        "1": "/bob/alice/doc.pdf"
    }


async def test_get_returns_only_requested_subset(store):
    for doc_id, path in (("1", "/a/1.pdf"), ("2", "/a/2.pdf"), ("3", "/a/3.pdf")):
        await store.upsert(
            user_id="alice", doc_id=doc_id, doc_type="file", file_path=path
        )
    # Only the two requested docs come back, keyed by doc_id.
    got = await store.get_paths_for_user("alice", "file", ["1", "3"])
    assert got == {"1": "/a/1.pdf", "3": "/a/3.pdf"}


async def test_get_tolerates_duplicate_and_missing_ids(store):
    await store.upsert(
        user_id="alice", doc_id="1", doc_type="file", file_path="/a/1.pdf"
    )
    # Duplicate (multiple chunks of one doc) + an id with no row.
    got = await store.get_paths_for_user("alice", "file", ["1", "1", "99"])
    assert got == {"1": "/a/1.pdf"}


async def test_get_scopes_by_doc_type(store):
    # A row for a different doc_type whose id collides with a file's id must NOT
    # be returned when querying files (guards the id-collision case).
    await store.upsert(
        user_id="alice", doc_id="1", doc_type="file", file_path="/a/file.pdf"
    )
    await store.upsert(
        user_id="alice", doc_id="1", doc_type="note", file_path="/a/note-row"
    )
    assert await store.get_paths_for_user("alice", "file", ["1"]) == {
        "1": "/a/file.pdf"
    }
    assert await store.get_paths_for_user("alice", "note", ["1"]) == {
        "1": "/a/note-row"
    }


async def test_delete_removes_only_that_user_row(store):
    await store.upsert(
        user_id="alice", doc_id="1", doc_type="file", file_path="/alice/doc.pdf"
    )
    await store.upsert(
        user_id="bob", doc_id="1", doc_type="file", file_path="/bob/alice/doc.pdf"
    )
    await store.delete(user_id="alice", doc_id="1", doc_type="file")
    assert await store.get_paths_for_user("alice", "file", ["1"]) == {}
    # Bob's row is untouched.
    assert await store.get_paths_for_user("bob", "file", ["1"]) == {
        "1": "/bob/alice/doc.pdf"
    }
