"""End-to-end per-user paths for a shared document (ADR-033, Deck #737).

Exercises the new code paths together against a real Nextcloud (share lookup +
verify-on-read) with an in-memory Qdrant standing in for the scanner:

- Phase 1/2: alice's file point stores HER path (owner-pinned scalar). bob, who
  the file is shared with, gets HIS own mount path substituted onto the result
  by ``verify_search_results`` from the ``document_paths`` store.
- Phase 3: the point carries ``folder_ancestors`` (the shared folder's canonical
  fileid). bob filtering on that folder matches; filtering on an unrelated folder
  id does not — a true left-anchored containment that is user-agnostic.

The pure filter/store/override matrices live in the unit suites
(``tests/unit/search/test_access_filter.py``,
``tests/unit/test_document_path_store.py``,
``tests/unit/search/test_verification.py``); this test is the glue proving the
real share → filter → verify → per-user-path chain.
"""

import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import BasicAuth
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider
from nextcloud_mcp_server.search.access_filter import (
    build_base_filter_conditions,
    clear_accessible_owners_cache,
    list_accessible_owners,
    resolve_prefix_folder_ids,
)
from nextcloud_mcp_server.search.semantic import SemanticSearchAlgorithm
from nextcloud_mcp_server.search.verification import verify_search_results
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.document_path_store import DocumentPathStore
from tests.integration.conftest import PDF_BYTES

pytestmark = pytest.mark.integration

_DOC_TEXT = "Confidential quarterly infrastructure budget and capacity plan"


def _user_client(username: str, password: str) -> NextcloudClient:
    return NextcloudClient(
        base_url=os.environ["NEXTCLOUD_HOST"],
        username=username,
        auth=BasicAuth(username, password),
        password=password,
    )


@pytest.fixture(autouse=True)
def _reset_owners_cache():
    clear_accessible_owners_cache()
    yield
    clear_accessible_owners_cache()


@pytest.fixture
async def acl_users(test_users_setup):
    clients = {
        name: _user_client(name, test_users_setup[name]["password"])
        for name in ("alice", "bob")
    }
    try:
        yield clients
    finally:
        for c in clients.values():
            await c._client.aclose()


@pytest.fixture
async def shared_file(acl_users):
    """alice creates a nested PDF, tags it, shares the containing folder with bob.

    Sharing the FOLDER (not just the file) means bob mounts it under his own root
    at a different path — the exact shape that produced the path thrash — and the
    folder keeps its canonical fileid for both users (the Phase 3 invariant).

    Yields (file_id, folder_id, alice_path, alice_folder_path).
    """
    alice = acl_users["alice"]
    suffix = uuid.uuid4().hex[:8]
    test_dir = f"share_paths_{suffix}"
    nested = f"{test_dir}/reports"
    path = f"{nested}/budget.pdf"

    await alice.webdav.create_directory(test_dir)
    await alice.webdav.create_directory(nested)
    await alice.webdav.write_file(path, PDF_BYTES, "application/pdf")
    file_id = (await alice.webdav.get_file_info(path))["id"]
    folder_id = await alice.webdav.get_fileid(f"/{nested}")
    tag = await alice.webdav.get_or_create_tag(
        name=get_settings().vector_sync_tag,
        user_visible=True,
        user_assignable=True,
    )
    await alice.webdav.assign_tag_to_file(file_id, tag["id"])
    # Share the top folder with bob (share_type=0 user, read permission).
    await alice.sharing.create_share(
        path=f"/{test_dir}", share_with="bob", share_type=0, permissions=1
    )
    try:
        yield file_id, folder_id, path, nested
    finally:
        try:
            await alice.webdav.remove_tag_from_file(file_id, tag["id"])
        except Exception:
            pass
        await alice.webdav.delete_resource(test_dir)


@pytest.fixture
async def path_store(monkeypatch):
    """A real DocumentPathStore on temp SQLite, wired into verify_search_results."""
    with tempfile.TemporaryDirectory() as tmp:
        storage = RefreshTokenStorage(db_path=str(Path(tmp) / "paths.db"))
        await storage.initialize()
        store = DocumentPathStore(storage)
        monkeypatch.setattr(DocumentPathStore, "shared", AsyncMock(return_value=store))
        yield store


@pytest.fixture
async def seeded(monkeypatch, shared_file):
    """In-memory Qdrant carrying alice's owner-pinned point with folder_ancestors."""
    file_id, folder_id, alice_path, _folder = shared_file
    provider = SimpleEmbeddingProvider(dimension=384)
    client = AsyncQdrantClient(":memory:")
    collection = get_settings().get_collection_name()
    await client.create_collection(
        collection_name=collection,
        vectors_config={"dense": VectorParams(size=384, distance=Distance.COSINE)},
    )
    await client.upsert(
        collection_name=collection,
        points=[
            PointStruct(
                id=int(file_id),
                vector={"dense": await provider.embed(_DOC_TEXT)},
                payload={
                    "doc_id": str(file_id),
                    "doc_type": "file",
                    "owner_id": "alice",
                    "user_id": "alice",
                    "is_placeholder": False,
                    # Owner-pinned scalar path (Phase 1): always alice's path.
                    "file_path": alice_path,
                    "title": "budget.pdf",
                    "excerpt": _DOC_TEXT,
                    "chunk_index": 0,
                    "total_chunks": 1,
                    # Phase 3: the shared folder's canonical fileid.
                    payload_keys.FOLDER_ANCESTORS: [folder_id],
                },
            )
        ],
        wait=True,
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.semantic.get_qdrant_client",
        AsyncMock(return_value=client),
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.semantic.get_embedding_service",
        lambda: provider,
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.context.get_qdrant_client",
        AsyncMock(return_value=client),
    )
    yield file_id, folder_id, alice_path
    await client.close()


async def _search_as(user_client, *, folder_ids=None):
    accessible_owners = await list_accessible_owners(
        user_client.sharing, user_client.username
    )
    algo = SemanticSearchAlgorithm(score_threshold=0.0)
    unverified = await algo.search(
        query=_DOC_TEXT,
        user_id=user_client.username,
        limit=10,
        doc_type="file",
        accessible_owners=accessible_owners,
        path_prefix_folder_ids=folder_ids,
    )
    kept, _dropped = await verify_search_results(user_client, unverified)
    return kept


async def _bob_share_mount(bob) -> str:
    """bob's own mount path for the folder alice shared with him (OCS file_target,
    e.g. ``/share_paths_<suffix>``) — the recipient-side path, resolved without
    the DAV meta endpoint (which does not resolve a shared file by global id)."""
    shares = await bob.sharing.list_shares(shared_with_me=True)
    for share in shares:
        target = str(share.get("file_target") or "").rstrip("/")
        if target.strip("/").startswith("share_paths_"):
            return target
    raise AssertionError("bob has no share_paths_* mount from alice")


async def test_recipient_sees_own_path_for_shared_file(acl_users, seeded, path_store):
    """bob's result shows HIS own stored path, not alice's owner-pinned scalar."""
    file_id, _folder_id, alice_path = seeded
    bob = acl_users["bob"]

    # bob's scanner would have recorded his own mount path for the shared file;
    # simulate that upsert with a path distinct from alice's owner-pinned scalar.
    bob_path = f"{await _bob_share_mount(bob)}/reports/budget.pdf"
    await path_store.upsert(
        user_id="bob", doc_id=str(file_id), doc_type="file", file_path=bob_path
    )

    kept = await _search_as(bob)
    assert kept, "bob should find the file alice shared with him"
    result = next(r for r in kept if r.id == str(file_id))
    # Phase 2: the displayed path is bob's own, substituted over the scalar.
    assert result.metadata["path"] == bob_path
    assert result.metadata["path"] != alice_path


async def test_owner_falls_back_to_scalar_path(acl_users, seeded, path_store):
    """alice (owner) has no store row, so the display path falls back to the
    Qdrant scalar — her own owner-pinned path (Phase 2 fallback)."""
    file_id, _folder_id, alice_path = seeded
    alice = acl_users["alice"]
    kept = await _search_as(alice)
    result = next(r for r in kept if r.id == str(file_id))
    assert result.metadata["path"] == alice_path


async def test_folder_filter_matches_by_ancestor_id(acl_users, seeded):
    """bob resolves the shared folder to the SAME canonical fileid as alice and
    filters on it to find the file; an unrelated id does not — true user-agnostic
    containment (Phase 3)."""
    file_id, folder_id, _alice_path = seeded
    bob = acl_users["bob"]

    # bob resolves his OWN view of the shared /reports folder to a fileid via the
    # production resolver — it must equal alice's canonical folder_id.
    bob_reports = f"{await _bob_share_mount(bob)}/reports"
    bob_folder_ids = await resolve_prefix_folder_ids(
        bob.webdav, path_prefixes=[bob_reports]
    )
    assert bob_folder_ids == [folder_id], (
        "a shared folder must resolve to one canonical fileid for every user"
    )

    kept = await _search_as(bob, folder_ids=bob_folder_ids)
    assert any(r.id == str(file_id) for r in kept)

    # An unrelated folder id must not match via folder_ancestors. (There is no
    # file_path prefix in this query, so no MatchText fallback can rescue it.)
    kept_none = await _search_as(bob, folder_ids=["999999999"])
    assert not any(r.id == str(file_id) for r in kept_none)


def test_filter_shape_prefers_folder_ancestor_over_matchtext():
    """Guard: a resolved folder id yields a folder_ancestors MatchAny branch."""
    conditions = build_base_filter_conditions(
        "bob", None, path_prefix="/x", path_prefix_folder_ids=["42"]
    )
    assert any(
        getattr(c, "key", None) == payload_keys.FOLDER_ANCESTORS
        or (
            hasattr(c, "should")
            and c.should
            and any(
                getattr(sc, "key", None) == payload_keys.FOLDER_ANCESTORS
                for sc in c.should
            )
        )
        for c in conditions
    )
