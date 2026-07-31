"""Result granularity — real Qdrant grouping through the hybrid algorithm.

``tests/unit/search/test_bm25_hybrid.py::TestGranularity`` asserts the *query
shape* sent to Qdrant (group_by, group_size, prefetch depth) against a mock.
That cannot show whether grouping actually changes what a caller receives, so
this exercises ``BM25HybridSearchAlgorithm.search()`` end to end against an
in-memory Qdrant holding a corpus deliberately shaped like the production one:
a chunk-heavy document that monopolises chunk-level results, plus several
single-chunk documents that it crowds out.

No Nextcloud, no verification layer, no background sync — fast and
deterministic.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from nextcloud_mcp_server.search.bm25_hybrid import BM25HybridSearchAlgorithm

pytestmark = pytest.mark.integration

_COLLECTION = "granularity_test"
_DIM = 4

# One "bundle" document with many chunks (the shape that monopolises a
# chunk-ranked result page) and four single-chunk documents.
_BUNDLE_CHUNKS = 6
_SINGLE_DOCS = ["single-1", "single-2", "single-3", "single-4"]


_QUERY_VEC = [1.0, 0.0, 0.0, 0.0]


def _bundle_vec(chunk_index: int) -> list[float]:
    """Bundle chunks sit closest to the query, so they legitimately win the
    chunk-level ranking — the production shape this mode addresses."""
    return [1.0, 0.01 * chunk_index, 0.0, 0.0]


def _single_vec(i: int) -> list[float]:
    """Single-chunk documents are relevant but rank below every bundle chunk."""
    return [1.0, 0.5 + (0.1 * i), 0.0, 0.0]


@pytest.fixture
async def granularity_collection(monkeypatch):
    client = AsyncQdrantClient(":memory:")
    await client.create_collection(
        collection_name=_COLLECTION,
        vectors_config={"dense": VectorParams(size=_DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams()},
    )

    points: list[PointStruct] = []
    pid = 0
    for chunk_index in range(_BUNDLE_CHUNKS):
        points.append(
            _point(
                pid,
                "bundle",
                chunk_index,
                _BUNDLE_CHUNKS,
                _bundle_vec(chunk_index),
                sparse_weight=1.0,
            )
        )
        pid += 1
    for i, doc_id in enumerate(_SINGLE_DOCS):
        points.append(_point(pid, doc_id, 0, 1, _single_vec(i), sparse_weight=0.5))
        pid += 1
    await client.upsert(collection_name=_COLLECTION, points=points, wait=True)

    settings = MagicMock()
    settings.get_collection_name.return_value = _COLLECTION
    settings.get_embedding_provider_family.return_value = "test"
    settings.vector_search_rrf_k = 60
    settings.acl_prefilter_enabled = False

    provider = MagicMock()
    provider.embed_with_usage = AsyncMock(return_value=(_QUERY_VEC, 3))
    bm25 = MagicMock()
    bm25.encode_async = AsyncMock(return_value={"indices": [1], "values": [1.0]})

    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_qdrant_client",
        AsyncMock(return_value=client),
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_provider", lambda: provider
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_bm25_service",
        AsyncMock(return_value=bm25),
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_settings", lambda: settings
    )

    yield client

    await client.close()


def _point(
    pid: int,
    doc_id: str,
    chunk_index: int,
    total: int,
    dense: list[float],
    *,
    sparse_weight: float,
) -> PointStruct:
    return PointStruct(
        id=pid,
        vector={
            "dense": dense,
            "sparse": SparseVector(indices=[1], values=[sparse_weight]),
        },
        payload={
            "doc_id": doc_id,
            "doc_type": "file",
            "user_id": "alice",
            "owner_id": "alice",
            "is_placeholder": False,
            "title": f"{doc_id}.pdf",
            "excerpt": f"chunk {chunk_index} of {doc_id}",
            "file_path": f"docs/{doc_id}.pdf",
            "chunk_index": chunk_index,
            "total_chunks": total,
            # Distinct offsets matter: search() deduplicates on
            # (doc_id, doc_type, chunk_start_offset, chunk_end_offset), so
            # leaving these unset would collapse every chunk of a document into
            # one row and hide the very concentration this suite is about.
            "chunk_start_offset": chunk_index * 1000,
            "chunk_end_offset": (chunk_index + 1) * 1000,
        },
    )


async def _search(granularity: str, limit: int = 5) -> list:
    return await BM25HybridSearchAlgorithm().search(
        query="anything",
        user_id="alice",
        limit=limit,
        doc_type="file",
        granularity=granularity,
    )


async def test_chunk_granularity_lets_one_document_monopolise(
    granularity_collection,
):
    """Baseline: the pre-existing behaviour this mode exists to offer an
    alternative to. The bundle's chunks crowd out the other documents."""
    results = await _search("chunk", limit=5)

    doc_ids = [r.id for r in results]
    assert doc_ids.count("bundle") > 1, (
        "chunk granularity is expected to return several chunks of the same "
        f"document — got {doc_ids}"
    )
    assert len(set(doc_ids)) < len(doc_ids), "expected duplicate documents"


async def test_document_granularity_returns_one_row_per_document(
    granularity_collection,
):
    results = await _search("document", limit=5)

    doc_ids = [r.id for r in results]
    assert len(doc_ids) == len(set(doc_ids)), (
        f"every document must appear at most once — got {doc_ids}"
    )
    assert doc_ids.count("bundle") == 1


async def test_document_granularity_surfaces_crowded_out_documents(
    granularity_collection,
):
    """The payoff: documents the bundle displaced now fit on the page."""
    chunk_docs = {r.id for r in await _search("chunk", limit=5)}
    document_docs = {r.id for r in await _search("document", limit=5)}

    assert len(document_docs) > len(chunk_docs)
    recovered = document_docs - chunk_docs
    assert recovered, "expected at least one previously-crowded-out document"
    assert recovered <= set(_SINGLE_DOCS)


async def test_document_granularity_returns_the_documents_best_chunk(
    granularity_collection,
):
    """The row that represents a document must be its top-scoring chunk, not an
    arbitrary one — the excerpt is what the caller reads."""
    grouped_bundle = next(
        r for r in await _search("document", limit=5) if r.id == "bundle"
    )
    # Rather than hard-coding which chunk "should" win (that would just restate
    # the fixture's vectors), assert the invariant: the representative is the
    # same chunk that ranks highest for this document at chunk granularity.
    best_ranked = next(r for r in await _search("chunk", limit=10) if r.id == "bundle")

    assert grouped_bundle.chunk_index == best_ranked.chunk_index
    assert grouped_bundle.excerpt == best_ranked.excerpt


async def test_limit_counts_documents_not_chunks(granularity_collection):
    """With 5 documents indexed, limit=3 must yield 3 documents."""
    results = await _search("document", limit=3)

    assert len(results) == 3
    assert len({r.id for r in results}) == 3
