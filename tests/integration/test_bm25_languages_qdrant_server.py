"""BM25_LANGUAGES end to end: real FastEmbed model, real Qdrant server (GH #1918).

Documents are encoded the way ingest encodes them (``encode_batch``), queries
the way search encodes them (``encode_async``), and matched by a sparse query
on a Qdrant server, so a stemmer or stopword mismatch between the two sides
shows up as a missed document. No dense provider is involved, which is the
keyword-only (``keyword-index``) path.

Skips when no Qdrant server answers on localhost:6333. The first run downloads
the ``Qdrant/bm25`` model from HuggingFace.
"""

import os
import uuid

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as m

from nextcloud_mcp_server.providers.bm25 import BM25SparseEmbeddingProvider

pytestmark = pytest.mark.integration

_URL = os.getenv("QDRANT_TEST_URL", "http://localhost:6333")
_API_KEY = os.getenv("QDRANT_API_KEY", "my_secret_api_key")

_DOCS = {
    1: "Обработка контекста: передайте file_id, и running jobs будут сохранены.",
    2: "Квартальный отчёт о продажах и бюджете отдела.",
    3: "Deployment notes for the ingest worker and its queue.",
}


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(url=_URL, api_key=_API_KEY, https=False)
    try:
        await client.get_collections()
    except Exception as e:  # noqa: BLE001 - no server: skip, not fail
        await client.close()
        pytest.skip(f"no Qdrant server at {_URL}: {e}")
    yield client
    await client.close()


async def _index(client: AsyncQdrantClient, provider) -> str:
    name = f"bm25_lang_{uuid.uuid4().hex[:8]}"
    await client.create_collection(
        name,
        vectors_config={},
        sparse_vectors_config={"sparse": m.SparseVectorParams()},
    )
    sparse = await provider.encode_batch(list(_DOCS.values()))
    await client.upsert(
        name,
        points=[
            m.PointStruct(id=doc_id, vector={"sparse": m.SparseVector(**vec)})
            for doc_id, vec in zip(_DOCS, sparse, strict=True)
        ],
        wait=True,
    )
    return name


async def _hits(client: AsyncQdrantClient, name: str, provider, query: str) -> set:
    vec = await provider.encode_async(query)
    result = await client.query_points(
        name, query=m.SparseVector(**vec), using="sparse", limit=10
    )
    return {p.id for p in result.points}


@pytest.mark.parametrize(
    ("languages", "query", "expected"),
    [
        # The bug: English-only stemming leaves Russian inflections unmatched.
        (("english",), "контекст", set()),
        (("english",), "контексту", set()),
        (("english",), "run", {1}),
        # Russian alone fixes Russian, but cannot stem English.
        (("russian",), "контексту", {1}),
        (("russian",), "run", set()),
        # Mixed: both languages stem, identifiers stay searchable.
        (("english", "russian"), "контекст", {1}),
        (("english", "russian"), "контексту", {1}),
        (("english", "russian"), "сохранить", {1}),
        (("english", "russian"), "run", {1}),
        (("english", "russian"), "file_id", {1}),
        (("english", "russian"), "отчёты", {2}),
        (("english", "russian"), "deploy", {3}),
    ],
)
async def test_bm25_language_matching(qdrant, languages, query, expected):
    provider = BM25SparseEmbeddingProvider(languages=languages)
    name = await _index(qdrant, provider)
    try:
        assert await _hits(qdrant, name, provider, query) == expected
    finally:
        await qdrant.delete_collection(name)


async def test_stopwords_of_every_language_are_dropped():
    """A query made only of stopwords (Russian ``и``, English ``the``) encodes to
    nothing, so it matches nothing rather than every document."""
    provider = BM25SparseEmbeddingProvider(languages=("english", "russian"))
    assert (await provider.encode_async("и the"))["indices"] == []
