"""algorithm="exact": every matching document, unranked (search/exact.py).

Runs against an in-memory Qdrant, so the filter Qdrant actually evaluates is
what is tested, not the shape of the filter object.
"""

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as m

from nextcloud_mcp_server.search import exact
from nextcloud_mcp_server.search.exact import ExactMatchAlgorithm

pytestmark = pytest.mark.unit


def _point(i: int, text: str, **payload) -> m.PointStruct:
    base = {
        "doc_id": str(i),
        "doc_type": "file",
        "title": f"doc {i}",
        "excerpt": text,
        "user_id": "alice",
        "owner_id": "alice",
        "is_placeholder": False,
        "chunk_index": 0,
        "total_chunks": 1,
    }
    return m.PointStruct(id=i, vector=[1.0, 0.0], payload={**base, **payload})


@pytest.fixture
async def qdrant(mocker):
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        "c", vectors_config=m.VectorParams(size=2, distance=m.Distance.COSINE)
    )
    mocker.patch.object(exact, "get_qdrant_client", return_value=client)
    settings = mocker.MagicMock()
    settings.get_collection_name.return_value = "c"
    mocker.patch.object(exact, "get_settings", return_value=settings)
    yield client
    await client.close()


async def test_returns_every_matching_document_beyond_any_page(qdrant):
    # More matches than one scroll page and than any ranked search returns.
    await qdrant.upsert(
        "c",
        [_point(i, f"letter {i} to Jane Doe about scanning") for i in range(1, 1201)]
        + [_point(5000, "nothing relevant"), _point(5001, "Jane wrote; Doe replied")],
    )
    results = await ExactMatchAlgorithm().search("jane doe", "alice", limit=10)

    assert len(results) == 1200
    assert {r.score for r in results} == {1.0}
    assert "5001" not in {r.id for r in results}  # words, not the phrase


async def test_one_row_per_document_from_its_first_matching_chunk(qdrant):
    await qdrant.upsert(
        "c",
        [
            _point(1, "Jane Doe, page two", doc_id="7", chunk_index=2),
            _point(2, "Jane Doe, page one", doc_id="7", chunk_index=1),
        ],
    )
    [result] = await ExactMatchAlgorithm().search("Jane Doe", "alice")

    assert result.id == "7"
    assert result.chunk_index == 1


async def test_respects_ownership_and_doc_type(qdrant):
    await qdrant.upsert(
        "c",
        [
            _point(1, "Jane Doe"),
            _point(2, "Jane Doe", user_id="bob", owner_id="bob"),
            _point(3, "Jane Doe", doc_type="note"),
        ],
    )
    results = await ExactMatchAlgorithm().search("Jane Doe", "alice", doc_type="file")

    assert [r.id for r in results] == ["1"]


async def test_stops_at_the_cap_and_says_so(qdrant):
    await qdrant.upsert("c", [_point(i, "Jane Doe") for i in range(1, 11)])
    algo = ExactMatchAlgorithm(max_documents=4)

    assert len(await algo.search("Jane Doe", "alice")) == 4
    assert algo.truncated


async def test_stops_at_the_chunk_budget_and_says_so(qdrant, mocker):
    # One document matching in many chunks must not scroll them all.
    await qdrant.upsert(
        "c", [_point(i, "Jane Doe", doc_id="1", chunk_index=i) for i in range(1, 11)]
    )
    mocker.patch.object(exact, "_SCROLL_PAGE", 2)
    algo = ExactMatchAlgorithm(max_chunks=3)

    results = await algo.search("Jane Doe", "alice")

    assert len(results) == 1
    assert algo.truncated


async def test_blank_query_matches_nothing(qdrant):
    await qdrant.upsert("c", [_point(1, "Jane Doe")])

    assert await ExactMatchAlgorithm().search("   ", "alice") == []
