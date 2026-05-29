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
from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider
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

    Yields the ``SimpleEmbeddingProvider`` so the test can build a query vector
    identical to the one the algorithm will generate.
    """
    provider = SimpleEmbeddingProvider(dimension=384)
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
        "nextcloud_mcp_server.search.semantic.get_embedding_service",
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
