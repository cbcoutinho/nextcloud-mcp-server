"""Reranking end to end through the pipeline stage, with a stubbed reranker.

``tests/unit/search/test_rerank.py`` asserts the stage's behaviour against a
stubbed client, and ``tests/unit/api/test_search_rerank_api.py`` pins the HTTP
contract. Neither shows that reranking actually changes what a caller receives
once real retrieval has produced the candidates, which is what this covers:
``BM25HybridSearchAlgorithm.search()`` against an in-memory Qdrant, then the
real ``rerank_results`` stage over its output.

The cross-encoder itself is stubbed — the gateway is not part of this repo's
docker-compose, so a live rerank cannot run in CI. What is real here is the
retrieval, the candidate pool, the reordering, and the degradation path.

No Nextcloud, no verification layer, no background sync.
"""

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from nextcloud_mcp_server.providers.gateway_rerank import (
    RerankedIndex,
    RerankError,
)
from nextcloud_mcp_server.search import rerank as rerank_mod
from nextcloud_mcp_server.search.bm25_hybrid import BM25HybridSearchAlgorithm
from nextcloud_mcp_server.search.rerank import RERANK_APPLIED, RERANK_DEGRADED

pytestmark = pytest.mark.integration

_COLLECTION = "rerank_test"
_DIM = 4
_QUERY_VEC = [1.0, 0.0, 0.0, 0.0]
_DOCS = 8


class _Settings:
    search_rerank_enabled = True
    embedding_gateway_url = "https://gw.example"
    search_rerank_model = "vendor/model"
    search_rerank_pool_size = 200
    search_rerank_timeout_seconds = 30.0
    search_rerank_max_concurrency = 1
    vector_search_rrf_k = 60

    def get_collection_name(self):
        return _COLLECTION

    def get_embedding_provider_family(self):
        return "test"


@pytest.fixture
async def rerank_collection():
    """A corpus where dense similarity descends with doc index, so retrieval
    order is known and any reordering is unambiguously the reranker's doing."""
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        collection_name=_COLLECTION,
        vectors_config={"dense": VectorParams(size=_DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams()},
    )
    points = []
    for i in range(_DOCS):
        # Decreasing alignment with _QUERY_VEC ⇒ decreasing retrieval rank.
        vec = [1.0 - i * 0.1, i * 0.1, 0.0, 0.0]
        points.append(
            PointStruct(
                id=i + 1,
                vector={
                    "dense": vec,
                    "sparse": SparseVector(indices=[i], values=[1.0]),
                },
                payload={
                    "doc_id": str(i),
                    "doc_type": "file",
                    "title": f"document {i}",
                    "excerpt": f"body text for document {i}",
                    "chunk_index": 0,
                    "total_chunks": 1,
                },
            )
        )
    await client.upsert(collection_name=_COLLECTION, points=points, wait=True)
    yield client
    await client.close()


@pytest.fixture(autouse=True)
def _clean_rerank_state():
    rerank_mod._reset_rerank_state()
    yield
    rerank_mod._reset_rerank_state()


def _patch_algo(monkeypatch, client):
    settings = _Settings()

    class _Provider:
        async def embed_with_usage(self, text):
            return _QUERY_VEC, 3

    class _Bm25:
        async def encode_async(self, text):
            return {"indices": [0], "values": [1.0]}

    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_settings", lambda: settings
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_provider", lambda: _Provider()
    )

    async def _get_bm25():
        return _Bm25()

    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_bm25_service", _get_bm25
    )

    async def _get_qdrant():
        return client

    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_qdrant_client", _get_qdrant
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.build_base_filter_conditions",
        lambda **kwargs: [],
    )
    return settings


def _stub_reranker(monkeypatch, *, ranking=None, raises=None):
    class _Client:
        model = "vendor/model"

        async def rerank(self, query, documents):
            if raises is not None:
                raise raises
            return ranking(documents) if callable(ranking) else ranking

    async def _get(_settings):
        return _Client()

    monkeypatch.setattr(rerank_mod, "_get_client", _get)


async def test_rerank_reorders_real_retrieval_output(rerank_collection, monkeypatch):
    """Retrieval returns documents in descending dense similarity; the reranker
    reverses that, and the caller sees the reranked order."""
    settings = _patch_algo(monkeypatch, rerank_collection)
    results = await BM25HybridSearchAlgorithm().search(
        query="anything", user_id="alice", limit=_DOCS
    )
    retrieval_order = [r.id for r in results]
    assert len(retrieval_order) >= 4, "fixture must produce a ranked pool"

    _stub_reranker(
        monkeypatch,
        ranking=lambda docs: [
            RerankedIndex(index=i, score=float(i)) for i in reversed(range(len(docs)))
        ],
    )

    out, outcome = await rerank_mod.rerank_results(
        results, "anything", settings=settings, surface="http"
    )

    assert outcome == RERANK_APPLIED
    assert [r.id for r in out] == list(reversed(retrieval_order))
    assert all(r.rerank_score is not None for r in out)
    # The retrieval score survives, so score_threshold keeps its meaning.
    assert all(r.score is not None for r in out)


async def test_degraded_rerank_preserves_exact_retrieval_order(
    rerank_collection, monkeypatch
):
    """A reranker outage must be invisible in the results other than the flag."""
    settings = _patch_algo(monkeypatch, rerank_collection)
    results = await BM25HybridSearchAlgorithm().search(
        query="anything", user_id="alice", limit=_DOCS
    )
    retrieval_order = [r.id for r in results]

    _stub_reranker(monkeypatch, raises=RerankError("gateway unavailable"))

    out, outcome = await rerank_mod.rerank_results(
        results, "anything", settings=settings, surface="http"
    )

    assert outcome == RERANK_DEGRADED
    assert [r.id for r in out] == retrieval_order
    assert all(r.rerank_score is None for r in out)


async def test_partial_ranking_keeps_every_candidate(rerank_collection, monkeypatch):
    """A provider that scores only part of the pool must not cost us recall."""
    settings = _patch_algo(monkeypatch, rerank_collection)
    results = await BM25HybridSearchAlgorithm().search(
        query="anything", user_id="alice", limit=_DOCS
    )
    n = len(results)

    _stub_reranker(monkeypatch, ranking=[RerankedIndex(index=n - 1, score=9.0)])

    out, outcome = await rerank_mod.rerank_results(
        results, "anything", settings=settings, surface="http"
    )

    assert outcome == RERANK_APPLIED
    assert len(out) == n, "unscored candidates are appended, never dropped"
    assert out[0].id == results[n - 1].id
