"""ADR-042 §6: hybrid search's image prefetch, on an in-memory Qdrant.

Real filters and the real prefetch ``score_threshold``, so what Qdrant
evaluates is tested: the floor abstains, ACL still applies to images, DBSF and
an image-side failure fall back to text only.
"""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as m

from nextcloud_mcp_server.search import bm25_hybrid
from nextcloud_mcp_server.search.algorithms import QueryEmbeddingUnavailable
from nextcloud_mcp_server.search.bm25_hybrid import BM25HybridSearchAlgorithm
from nextcloud_mcp_server.vector import payload_keys

pytestmark = pytest.mark.unit

TEXT, IMAGE = [1.0, 0.0], [0.0, 1.0]
BASE = {
    "doc_type": "file",
    "user_id": "alice",
    "owner_id": "alice",
    "is_placeholder": False,
    "total_chunks": 1,
}


@pytest.fixture
async def env(monkeypatch):
    client = AsyncQdrantClient(location=":memory:")
    vec = m.VectorParams(size=2, distance=m.Distance.COSINE)
    await client.create_collection(
        "c",
        vectors_config={"dense": vec, "image": vec},
        sparse_vectors_config={"sparse": m.SparseVectorParams()},
    )
    sparse = m.SparseVector(indices=[1], values=[1.0])
    img = {payload_keys.VECTOR_KIND: payload_keys.VECTOR_KIND_IMAGE}
    await client.upsert(
        "c",
        [
            m.PointStruct(
                id=1,
                vector={"dense": TEXT, "sparse": sparse},
                payload={
                    **BASE,
                    "doc_id": "1",
                    "title": "notes",
                    "chunk_index": 0,
                    "excerpt": "text",
                },
            ),
            m.PointStruct(
                id=2,
                vector={"image": IMAGE},
                payload={**BASE, "doc_id": "2", "title": "photo.png", **img},
            ),
            m.PointStruct(  # bob's image: ACL must hold for the image prefetch
                id=3,
                vector={"image": IMAGE},
                payload={
                    **BASE,
                    "doc_id": "3",
                    "title": "bob.png",
                    "user_id": "bob",
                    "owner_id": "bob",
                    **img,
                },
            ),
        ],
    )
    provider = MagicMock()
    provider.embed_with_usage = AsyncMock(return_value=(TEXT, 3))
    provider.embed_for_image_space = AsyncMock(return_value=IMAGE)
    bm25 = MagicMock()
    bm25.encode_async = AsyncMock(return_value={"indices": [1], "values": [1.0]})
    settings = MagicMock()
    settings.get_collection_name.return_value = "c"
    settings.get_embedding_provider_family.return_value = "simple"
    settings.vector_search_rrf_k = 60
    settings.search_embedding_timeout_seconds = 10.0
    settings.image_search_score_threshold = 0.5
    settings.image_embedding_model = "simple-image-hash"
    record = MagicMock()
    for name, value in {
        "get_provider": lambda: provider,
        "get_bm25_service": AsyncMock(return_value=bm25),
        "get_qdrant_client": AsyncMock(return_value=client),
        "get_settings": lambda: settings,
        "image_vector_ready": lambda: True,
        "record_image_search": record,
    }.items():
        monkeypatch.setattr(bm25_hybrid, name, value)
    yield provider, record
    await client.close()


def _ids(results) -> dict[str, str | None]:
    return {r.id: r.metadata.get("match") for r in results}


async def test_matching_query_returns_the_users_image(env):
    _, record = env
    results = await BM25HybridSearchAlgorithm().search("q", "alice")
    assert _ids(results) == {"1": None, "2": "image"}  # never bob's "3"
    record.assert_called_once_with("simple-image-hash", 1)


async def test_floor_abstains_for_an_unrelated_query(env):
    provider, record = env
    provider.embed_for_image_space.return_value = TEXT  # cosine 0 < 0.5
    results = await BM25HybridSearchAlgorithm().search("q", "alice")
    assert _ids(results) == {"1": None}
    record.assert_called_once_with("simple-image-hash", 0)


async def test_dbsf_searches_text_only(env):
    provider, record = env
    results = await BM25HybridSearchAlgorithm(fusion="dbsf").search("q", "alice")
    assert _ids(results) == {"1": None}
    provider.embed_for_image_space.assert_not_awaited()
    record.assert_not_called()


async def test_image_embedding_failure_keeps_text_results(env):
    provider, record = env
    provider.embed_for_image_space.side_effect = RuntimeError("down")
    results = await BM25HybridSearchAlgorithm().search("q", "alice")
    assert _ids(results) == {"1": None}
    record.assert_not_called()


async def test_dense_failure_is_not_wrapped_in_an_exception_group(env):
    provider, _ = env
    provider.embed_with_usage.side_effect = TimeoutError()
    with pytest.raises(QueryEmbeddingUnavailable):
        await BM25HybridSearchAlgorithm().search("q", "alice")


async def test_dense_failure_cancels_the_image_embedding(env):
    """A dense failure doesn't wait out (and pay for) the image call."""
    provider, _ = env
    cancelled = anyio.Event()

    async def hang(_query):
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()

    provider.embed_for_image_space.side_effect = hang
    provider.embed_with_usage.side_effect = TimeoutError()
    with anyio.fail_after(5), pytest.raises(QueryEmbeddingUnavailable):
        await BM25HybridSearchAlgorithm().search("q", "alice")
    assert cancelled.is_set()


async def test_off_when_the_collection_has_no_image_vector(env, monkeypatch):
    provider, _ = env
    monkeypatch.setattr(bm25_hybrid, "image_vector_ready", lambda: False)
    results = await BM25HybridSearchAlgorithm().search("q", "alice")
    assert _ids(results) == {"1": None}
    provider.embed_for_image_space.assert_not_awaited()


async def test_image_query_embedded_once_across_doc_types(env):
    provider, _ = env
    algo = BM25HybridSearchAlgorithm()
    await algo.search("q", "alice", doc_type="file")
    await algo.search("q", "alice", doc_type="note")
    assert provider.embed_for_image_space.await_count == 1


async def test_a_failed_image_embedding_is_not_retried_per_doc_type(env):
    provider, _ = env
    provider.embed_for_image_space.side_effect = RuntimeError("down")
    algo = BM25HybridSearchAlgorithm()
    await algo.search("q", "alice", doc_type="file")
    await algo.search("q", "alice", doc_type="note")
    assert provider.embed_for_image_space.await_count == 1


async def test_an_image_hit_converts_to_the_public_model(env):
    """No chunk fields on an image point: the tool must not choke on None."""
    from nextcloud_mcp_server.server.semantic import _to_semantic_results

    results = await BM25HybridSearchAlgorithm().search("q", "alice")
    [image] = [r for r in results if r.metadata.get("match") == "image"]
    [row] = _to_semantic_results(
        [image], browser_base=None, fusion="rrf", algorithm="hybrid", rerank_model=None
    )
    assert (row.match, row.excerpt, row.chunk_index, row.total_chunks) == (
        "image",
        "",
        0,
        1,
    )


async def test_a_file_with_text_and_image_returns_both_hits(env):
    """Dedup keys on chunk offsets; the image point has none, so it can't
    collapse into the same file's chunk-0 text hit."""
    client = await bm25_hybrid.get_qdrant_client()
    await client.upsert(
        "c",
        [
            m.PointStruct(
                id=4,
                vector={
                    "dense": TEXT,
                    "sparse": m.SparseVector(indices=[1], values=[1.0]),
                },
                payload={
                    **BASE,
                    "doc_id": "2",
                    "title": "photo.png",
                    "chunk_index": 0,
                    "chunk_start_offset": 0,
                    "chunk_end_offset": 4,
                    "excerpt": "text",
                },
            )
        ],
    )
    results = await BM25HybridSearchAlgorithm().search("q", "alice")
    assert sorted(
        r.metadata.get("match") or "text" for r in results if r.id == "2"
    ) == [
        "image",
        "text",
    ]
