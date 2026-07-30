"""ACL-aware ownership filter — deterministic, in-memory Qdrant.

Proves the query-time ownership expansion added for ACL-aware search
(``search/access_filter.build_ownership_filter`` →
``SemanticSearchAlgorithm``): a user finds documents whose owner shared them
(``owner_id`` ∈ accessible_owners), does not find documents owned by users who
have not shared with them, and legacy points carrying only ``user_id`` stay
findable by their original indexer.

This complements ``tests/unit/search/test_access_filter.py`` (filter
construction in isolation) by exercising the filter against a real Qdrant
engine through the actual search algorithm — no Nextcloud, no verification
layer, no background sync, so it is fast and deterministic. The full
real-Nextcloud flow (share + verify-on-read) lives in
``test_acl_shared_search.py``.
"""

from unittest.mock import AsyncMock

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.providers import SimpleProvider
from nextcloud_mcp_server.search.algorithms import get_indexed_doc_types
from nextcloud_mcp_server.search.context import _get_chunk_by_index_from_qdrant
from nextcloud_mcp_server.search.semantic import SemanticSearchAlgorithm

pytestmark = pytest.mark.integration

# Same text for every point so cosine similarity to the query is ~identical:
# the *filter*, not the score, must decide what each user sees.
_DOC_TEXT = "Quarterly infrastructure budget planning and resource allocation"

# (point_id, doc_id, owner_id, user_id) — owner_id=None mimics a legacy point
# indexed before the owner_id payload field existed.
_ALICE_FILE = (101, "101", "alice", "alice")
_CHARLIE_FILE = (102, "102", "charlie", "charlie")
_LEGACY_DAVE_FILE = (103, "103", None, "dave")


@pytest.fixture
async def seeded_collection(monkeypatch):
    """In-memory Qdrant seeded with three file points, wired into the algorithm.

    Yields the ``SimpleProvider`` so the test can build a query vector
    identical to the one the algorithm will generate.
    """
    provider = SimpleProvider(dimension=384)
    client = AsyncQdrantClient(":memory:")
    collection = get_settings().get_collection_name()

    # The production collection uses a named "dense" vector (see
    # vector/qdrant_client.py); the semantic algorithm queries using="dense".
    await client.create_collection(
        collection_name=collection,
        vectors_config={"dense": VectorParams(size=384, distance=Distance.COSINE)},
    )

    embedding = await provider.embed(_DOC_TEXT)
    points = []
    for point_id, doc_id, owner_id, user_id in (
        _ALICE_FILE,
        _CHARLIE_FILE,
        _LEGACY_DAVE_FILE,
    ):
        payload = {
            "doc_id": doc_id,
            "doc_type": "file",
            "user_id": user_id,
            "is_placeholder": False,
            "file_path": f"docs/{doc_id}.txt",
            "title": f"file {doc_id}",
            "excerpt": _DOC_TEXT,
            "chunk_index": 0,
            "total_chunks": 1,
        }
        # Legacy points carry no owner_id at all.
        if owner_id is not None:
            payload["owner_id"] = owner_id
        points.append(
            PointStruct(id=point_id, vector={"dense": embedding}, payload=payload)
        )

    await client.upsert(collection_name=collection, points=points, wait=True)

    # Point the algorithm at the in-memory client + deterministic embeddings.
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.semantic.get_qdrant_client",
        AsyncMock(return_value=client),
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.semantic.get_provider",
        lambda: provider,
    )
    # get_indexed_doc_types reads the client from the algorithms module.
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.algorithms.get_qdrant_client",
        AsyncMock(return_value=client),
    )
    # The cached-chunk lookups read the client from the context module.
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.context.get_qdrant_client",
        AsyncMock(return_value=client),
    )

    yield provider

    await client.close()


def _ids(results):
    return {r.id for r in results}


async def test_shared_owner_is_visible_unshared_is_not(seeded_collection):
    """Bob sees Alice's file (shared → owner in accessible_owners), not Charlie's."""
    algo = SemanticSearchAlgorithm(score_threshold=0.0)

    results = await algo.search(
        query=_DOC_TEXT,
        user_id="bob",
        limit=10,
        doc_type="file",
        accessible_owners=["bob", "alice"],
    )

    found = _ids(results)
    assert "101" in found, "Alice's shared file must be discoverable by Bob"
    assert "102" not in found, "Charlie's unshared file must NOT be visible to Bob"
    assert "103" not in found, "Legacy file owned by dave must NOT be visible to Bob"


async def test_no_shares_sees_only_own(seeded_collection):
    """With no shares, Bob (who owns nothing here) gets nothing."""
    algo = SemanticSearchAlgorithm(score_threshold=0.0)

    results = await algo.search(
        query=_DOC_TEXT,
        user_id="bob",
        limit=10,
        doc_type="file",
        accessible_owners=["bob"],
    )

    assert _ids(results) == set()


async def test_legacy_user_id_point_still_found_by_indexer(seeded_collection):
    """A pre-owner_id point stays findable by its original indexer via the
    legacy ``user_id`` OR-branch in build_ownership_filter."""
    algo = SemanticSearchAlgorithm(score_threshold=0.0)

    results = await algo.search(
        query=_DOC_TEXT,
        user_id="dave",
        limit=10,
        doc_type="file",
        accessible_owners=["dave"],
    )

    found = _ids(results)
    assert "103" in found, "dave must still find his own legacy (user_id-only) file"
    assert "101" not in found
    assert "102" not in found


async def test_owner_sees_own_new_style_point(seeded_collection):
    """Alice finds her own file via the owner_id branch."""
    algo = SemanticSearchAlgorithm(score_threshold=0.0)

    results = await algo.search(
        query=_DOC_TEXT,
        user_id="alice",
        limit=10,
        doc_type="file",
        accessible_owners=["alice"],
    )

    found = _ids(results)
    assert "101" in found
    assert "102" not in found
    assert "103" not in found


async def test_get_indexed_doc_types_is_acl_aware(seeded_collection):
    """get_indexed_doc_types respects the ownership scope: with the expanded
    accessible_owners Bob discovers the shared "file" type, but self-only Bob
    (who owns nothing here) discovers nothing — proving it is no longer
    ACL-blind."""
    # ACL-aware: Bob can read Alice's shared file → discovers "file".
    assert await get_indexed_doc_types("bob", accessible_owners=["bob", "alice"]) == {
        "file"
    }
    # Self-only (default): Bob owns nothing here → discovers nothing.
    assert await get_indexed_doc_types("bob") == set()


async def test_cached_chunk_lookup_is_acl_aware(seeded_collection):
    """The cached-chunk Qdrant lookup honours accessible_owners: Bob retrieves
    the excerpt of Alice's file point (owner_id=alice, chunk_index=0) when alice
    is in his accessible owners, but not when scoped self-only. This is the
    Qdrant-layer half of cross-user file chunk context (the per-file access
    gate lives in get_chunk_with_context / file_accessible_by_id)."""
    # Alice's seeded file point (_ALICE_FILE) carries excerpt=_DOC_TEXT at chunk 0.
    text = await _get_chunk_by_index_from_qdrant(
        "bob", "101", "file", 0, accessible_owners=["bob", "alice"]
    )
    assert text == _DOC_TEXT
    # Self-only Bob cannot reach Alice's cached chunk.
    assert await _get_chunk_by_index_from_qdrant("bob", "101", "file", 0) is None


# --- share-root narrowing (real Qdrant evaluation of the nested filter) ------
#
# tests/unit/search/test_access_filter.py asserts the Filter OBJECT's shape.
# These cases run that filter through a real Qdrant engine, which is the only
# way to pin how the nested must/should + IsEmptyCondition combination actually
# evaluates — including the non-file behaviour documented on
# build_ownership_filter.

_SHARED_FOLDER_ID = "900"
_OTHER_FOLDER_ID = "999"


@pytest.fixture
async def narrowing_collection(monkeypatch):
    """In-memory Qdrant seeded with points that differ only in containment.

    Every point is owned by ``alice`` and carries identical text, so the
    ownership filter — not the score, doc_type, or owner — decides visibility.
    """
    provider = SimpleProvider(dimension=384)
    client = AsyncQdrantClient(":memory:")
    collection = get_settings().get_collection_name()

    await client.create_collection(
        collection_name=collection,
        vectors_config={"dense": VectorParams(size=384, distance=Distance.COSINE)},
    )

    embedding = await provider.embed(_DOC_TEXT)
    # (point_id, doc_id, doc_type, folder_ancestors)
    seed = [
        (201, "201", "file", [_SHARED_FOLDER_ID]),  # inside the shared folder
        (202, "202", "file", [_OTHER_FOLDER_ID]),  # outside it
        (203, _SHARED_FOLDER_ID, "file", []),  # the shared folder's own point
        (204, "204", "file", []),  # pre-ADR-033 file: no ancestors resolved
        (205, "205", "note", []),  # non-file: never gets ancestors
    ]
    points = [
        PointStruct(
            id=pid,
            vector={"dense": embedding},
            payload={
                "doc_id": doc_id,
                "doc_type": doc_type,
                "user_id": "alice",
                "owner_id": "alice",
                "folder_ancestors": ancestors,
                "is_placeholder": False,
                "file_path": f"docs/{doc_id}.txt",
                "title": f"doc {doc_id}",
                "excerpt": _DOC_TEXT,
                "chunk_index": 0,
                "total_chunks": 1,
            },
        )
        for pid, doc_id, doc_type, ancestors in seed
    ]
    await client.upsert(collection_name=collection, points=points, wait=True)

    monkeypatch.setattr(
        "nextcloud_mcp_server.search.semantic.get_qdrant_client",
        AsyncMock(return_value=client),
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.semantic.get_provider", lambda: provider
    )

    yield provider

    await client.close()


async def _search_as_bob(shared_root_ids, doc_type=None):
    """Bob searching alice-owned content (alice shared *something* with him)."""
    algo = SemanticSearchAlgorithm(score_threshold=0.0)
    return _ids(
        await algo.search(
            query=_DOC_TEXT,
            user_id="bob",
            limit=10,
            doc_type=doc_type,
            accessible_owners=["bob", "alice"],
            shared_root_ids=shared_root_ids,
        )
    )


async def test_without_share_roots_whole_owner_corpus_is_admitted(
    narrowing_collection,
):
    """Baseline (pre-fix behaviour): one share exposes everything alice owns."""
    found = await _search_as_bob(shared_root_ids=None, doc_type="file")

    assert {"201", "202", "204", _SHARED_FOLDER_ID} <= found


async def test_share_roots_admit_only_the_shared_subtree(narrowing_collection):
    """The point outside the shared folder is filtered out by Qdrant itself."""
    found = await _search_as_bob(shared_root_ids=[_SHARED_FOLDER_ID], doc_type="file")

    assert "201" in found, "file inside the shared folder must be admitted"
    assert "202" not in found, (
        "file outside the shared folder must NOT be admitted — this is the "
        "candidate-pool pollution the narrowing removes"
    )


async def test_shared_folder_own_point_is_admitted_via_doc_id(narrowing_collection):
    """A folder does not list itself in folder_ancestors — the doc_id branch
    is what keeps the shared item itself (and single-file shares) findable."""
    found = await _search_as_bob(shared_root_ids=[_SHARED_FOLDER_ID], doc_type="file")

    assert _SHARED_FOLDER_ID in found


async def test_file_without_resolved_ancestors_fails_open(narrowing_collection):
    """Pre-ADR-033 files carry no ancestors; dropping them would lose real
    recall, so the filter fails open and lets verify-on-read decide."""
    found = await _search_as_bob(shared_root_ids=[_SHARED_FOLDER_ID], doc_type="file")

    assert "204" in found


async def test_non_file_doc_types_are_not_narrowed(narrowing_collection):
    """Known limitation, pinned deliberately.

    ``folder_ancestors`` is only populated for ``doc_type == "file"``
    (vector/processor.py); every other type is stamped with ``[]``, which
    Qdrant's IsEmptyCondition cannot distinguish from "absent". So notes,
    deck cards, calendar entries etc. keep the old owner-level width and are
    still admitted even when they sit outside every shared root. Narrowing
    them needs ancestors populated for those types — tracked separately, not
    silently fixed here.
    """
    found = await _search_as_bob(shared_root_ids=[_SHARED_FOLDER_ID], doc_type="note")

    assert "205" in found
