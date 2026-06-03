"""End-to-end ACL-aware semantic search against a real Nextcloud (PR #813).

This is the card-120 acceptance criterion exercised across the *new* code
paths together:

1. ``list_accessible_owners`` resolves the querying user's real OCS shares into
   the set of owner UIDs they may search.
2. ``SemanticSearchAlgorithm`` applies the expanded ownership filter in Qdrant.
3. ``verify_search_results`` re-checks each hit against real Nextcloud
   (ACL-aware, by global file id).

Qdrant is in-memory and seeded directly with one point owned by *alice* — this
deliberately stands in for the background scanner (whose only relevant change
is writing ``owner_id`` into the payload, covered separately). Nextcloud itself
is real, so the share lookup (step 1) and the verification (step 3) exercise
the live OCS Sharing + WebDAV APIs. The result: bob, with whom alice shared the
file, finds it without having indexed anything; diana, with no share, does not.

The pure-filter matrix lives in ``test_acl_owner_filter.py`` and the
verification layer in ``test_verify_on_read.py``; this test is the glue that
proves the real share → accessible_owners → filter → verify chain.
"""

import os
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import BasicAuth
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider
from nextcloud_mcp_server.search.access_filter import (
    clear_accessible_owners_cache,
    list_accessible_owners,
)
from nextcloud_mcp_server.search.context import get_chunk_with_context
from nextcloud_mcp_server.search.semantic import SemanticSearchAlgorithm
from nextcloud_mcp_server.search.verification import verify_search_results
from tests.integration.conftest import PDF_BYTES

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_owners_cache():
    """Reset the process-global accessible-owners cache around each test so a
    real OCS share created in a fixture isn't masked by a stale cached entry."""
    clear_accessible_owners_cache()
    yield
    clear_accessible_owners_cache()


_DOC_TEXT = "Confidential quarterly infrastructure budget and capacity plan"


def _user_client(username: str, password: str) -> NextcloudClient:
    return NextcloudClient(
        base_url=os.environ["NEXTCLOUD_HOST"],
        username=username,
        auth=BasicAuth(username, password),
        password=password,
    )


@pytest.fixture
async def acl_users(test_users_setup):
    """alice (owner), bob (recipient), diana (no access) direct clients."""
    clients = {
        name: _user_client(name, test_users_setup[name]["password"])
        for name in ("alice", "bob", "diana")
    }
    try:
        yield clients
    finally:
        for c in clients.values():
            await c._client.aclose()


@pytest.fixture
async def shared_file(acl_users):
    """alice creates a nested PDF, tags it ``vector-index``, and shares it with
    bob (not diana).

    The vector-index tag is required because verify-on-read now gates file
    results on current tag membership (in addition to ACL access). The tag is
    created userVisible so the owner's assignment surfaces in the recipient's
    systemtag REPORT — this fixture is the live check of that assumption.

    Yields (file_id, owner_relative_path); cleans up the directory after.
    """
    alice = acl_users["alice"]
    suffix = uuid.uuid4().hex[:8]
    test_dir = f"acl_e2e_{suffix}"
    nested = f"{test_dir}/reports"
    path = f"{nested}/budget.pdf"

    await alice.webdav.create_directory(test_dir)
    await alice.webdav.create_directory(nested)
    await alice.webdav.write_file(path, PDF_BYTES, "application/pdf")
    file_id = (await alice.webdav.get_file_info(path))["id"]
    tag = await alice.webdav.get_or_create_tag(
        name=get_settings().vector_sync_pdf_tag,
        user_visible=True,
        user_assignable=True,
    )
    await alice.webdav.assign_tag_to_file(file_id, tag["id"])
    await alice.sharing.create_share(
        path=f"/{path}", share_with="bob", share_type=0, permissions=1
    )
    try:
        yield file_id, path
    finally:
        try:
            await alice.webdav.remove_tag_from_file(file_id, tag["id"])
        except Exception:
            pass
        await alice.webdav.delete_resource(test_dir)


@pytest.fixture
async def seeded_semantic(monkeypatch, shared_file):
    """In-memory Qdrant carrying alice's file point, wired into the algorithm.

    Stands in for the background scanner: the point carries ``owner_id=alice``
    exactly as the scanner now writes it.
    """
    file_id, path = shared_file
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
                    "file_path": path,
                    "title": "budget.pdf",
                    "excerpt": _DOC_TEXT,
                    "chunk_index": 0,
                    "total_chunks": 1,
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
    # The cached-chunk lookups (get_chunk_with_context) read the client from the
    # context module — point it at the same in-memory Qdrant.
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.context.get_qdrant_client",
        AsyncMock(return_value=client),
    )
    yield file_id
    await client.close()


async def _search_as(user_client, file_id_unused) -> list:
    """Run the full new chain (share lookup → filter → verify) as a user."""
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
    )
    kept, _dropped = await verify_search_results(user_client, unverified)
    return kept


async def test_recipient_finds_shared_file_without_indexing(acl_users, seeded_semantic):
    """Bob finds alice's shared file end-to-end: real share lookup expands his
    accessible owners to include alice, the filter surfaces her point, and
    real verification confirms both his ACL access AND that the file is still
    in the vector-index tag set — all without bob indexing.

    This also exercises the strict tag-gate's key assumption: a vector-index
    tag alice assigned (userVisible) surfaces in bob's systemtag REPORT for a
    file shared into his tree. If a future Nextcloud version stops surfacing an
    owner's tag to a recipient, this assertion is where it fails first."""
    file_id = seeded_semantic
    # Sanity: the live OCS lookup really does expand bob to include alice.
    owners = await list_accessible_owners(acl_users["bob"].sharing, "bob")
    assert "alice" in owners, "OCS shared-with-me must surface alice as an owner"

    kept = await _search_as(acl_users["bob"], file_id)

    assert [r.id for r in kept] == [str(file_id)], (
        "bob must find alice's shared file via semantic search"
    )


async def test_non_recipient_does_not_find_file(acl_users, seeded_semantic):
    """Diana, with no share, never sees the file: her accessible-owners set
    excludes alice, so the ownership filter drops the point before verification."""
    owners = await list_accessible_owners(acl_users["diana"].sharing, "diana")
    assert "alice" not in owners

    kept = await _search_as(acl_users["diana"], seeded_semantic)

    assert kept == [], "diana (no share) must not find alice's file"


async def test_file_accessible_by_id_resolves_shares(acl_users, shared_file):
    """Lock the verify-on-read contract directly on ``file_accessible_by_id``.

    The WebDAV SEARCH-by-fileid with ``scope=""`` must resolve a file that the
    caller does NOT own but which is shared with them. This is the exact check
    verify-on-read depends on for shared, nested files; a Nextcloud change to
    how ``scope=""`` is interpreted would otherwise silently break ACL-aware
    verification. The file lives in a subfolder, so a path-based check would
    404 for the recipient — only the by-id SEARCH gets it right.
    """
    file_id, _path = shared_file
    fid = int(file_id)

    # Owner and share recipient can both reach it...
    assert await acl_users["alice"].webdav.file_accessible_by_id(fid) is True
    assert await acl_users["bob"].webdav.file_accessible_by_id(fid) is True
    # ...the non-recipient cannot.
    assert await acl_users["diana"].webdav.file_accessible_by_id(fid) is False


async def test_cross_user_file_chunk_context(acl_users, seeded_semantic):
    """End-to-end cross-user FILE chunk context: Bob (a share recipient) gets
    Alice's cached chunk text, Diana (no share) gets None.

    Exercises the full secure path: the ACL-aware Qdrant cached-chunk lookup
    (owner_id=alice surfaces for Bob) gated by a real per-file
    ``file_accessible_by_id`` check against live Nextcloud. Diana fails the gate
    and is denied even though the chunk is cached. Per-user types are covered by
    the self-only behaviour elsewhere — this is the file path the feature adds.
    """
    file_id = seeded_semantic
    bob = acl_users["bob"]
    diana = acl_users["diana"]

    bob_owners = await list_accessible_owners(bob.sharing, "bob")
    assert "alice" in bob_owners

    ctx = await get_chunk_with_context(
        nc_client=bob,
        user_id="bob",
        doc_id=str(file_id),
        doc_type="file",
        chunk_start=0,
        chunk_end=len(_DOC_TEXT),
        chunk_index=0,
        total_chunks=1,
        accessible_owners=bob_owners,
    )
    assert ctx is not None, "Bob (share recipient) must get Alice's cached chunk"
    assert ctx.chunk_text == _DOC_TEXT

    # Diana has no share → per-file gate denies even though the chunk is cached.
    diana_owners = await list_accessible_owners(diana.sharing, "diana")
    denied = await get_chunk_with_context(
        nc_client=diana,
        user_id="diana",
        doc_id=str(file_id),
        doc_type="file",
        chunk_start=0,
        chunk_end=len(_DOC_TEXT),
        chunk_index=0,
        total_chunks=1,
        accessible_owners=diana_owners,
    )
    assert denied is None, "Diana (no share) must not get cross-user chunk context"
