"""algorithm="exact" against a real Qdrant server (the compose ``qdrant`` profile).

The unit tests run the phrase filter on the in-memory client, which matches by
its own Python implementation. A server evaluates ``MatchPhrase`` against the
TEXT index built from ``_EXCERPT_INDEX`` (tokenizer, lowercasing, positions), so
this is where "every document containing the phrase" is actually proven, with
the index the startup migration creates.

Skips when no Qdrant server answers on localhost:6333.
"""

import os
import uuid

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as m

from nextcloud_mcp_server.search import exact
from nextcloud_mcp_server.search.exact import ExactMatchAlgorithm
from nextcloud_mcp_server.vector.qdrant_client import (
    _EXCERPT_INDEX,
    _PAYLOAD_INDEX_FIELDS,
)

pytestmark = pytest.mark.integration

_URL = os.getenv("QDRANT_TEST_URL", "http://localhost:6333")
_API_KEY = os.getenv("QDRANT_API_KEY", "my_secret_api_key")


def _point(i: int, text: str, **payload) -> m.PointStruct:
    return m.PointStruct(
        id=i,
        vector=[1.0, 0.0],
        payload={
            "doc_id": str(i),
            "doc_type": "file",
            "title": f"doc {i}",
            "excerpt": text,
            "user_id": "alice",
            "owner_id": "alice",
            "is_placeholder": False,
            "chunk_index": 0,
            "total_chunks": 1,
            **payload,
        },
    )


@pytest.fixture
async def server_collection(mocker):
    client = AsyncQdrantClient(url=_URL, api_key=_API_KEY, https=False)
    try:
        await client.get_collections()
    except Exception as e:  # noqa: BLE001 - no server: skip, not fail
        await client.close()
        pytest.skip(f"no Qdrant server at {_URL}: {e}")
    name = f"exact_{uuid.uuid4().hex[:8]}"
    await client.create_collection(
        name, vectors_config=m.VectorParams(size=2, distance=m.Distance.COSINE)
    )
    # The indexes the startup migration creates, the excerpt one awaited here
    # so the test runs against the built index rather than a scan.
    for field, schema in {**_PAYLOAD_INDEX_FIELDS, "excerpt": _EXCERPT_INDEX}.items():
        await client.create_payload_index(
            name, field_name=field, field_schema=schema, wait=True
        )
    mocker.patch.object(exact, "get_qdrant_client", return_value=client)
    settings = mocker.MagicMock()
    settings.get_collection_name.return_value = name
    mocker.patch.object(exact, "get_settings", return_value=settings)
    yield client, name
    await client.delete_collection(name)
    await client.close()


async def test_every_document_with_the_phrase_and_no_other(server_collection):
    client, name = server_collection
    matching = [
        _point(i, f"Re: scanning. Dear Jane Doe, letter {i}.") for i in range(1, 1101)
    ]
    others = [
        _point(5001, "Jane wrote to Doe"),  # both words, not the phrase
        _point(5002, "Doe, Jane"),  # reversed
        _point(5003, "nothing relevant"),
    ]
    await client.upsert(name, matching + others, wait=True)

    results = await ExactMatchAlgorithm().search("jane doe", "alice")

    assert len(results) == 1100
    assert not {"5001", "5002", "5003"} & {r.id for r in results}


async def test_phrase_matches_across_case_and_line_breaks(server_collection):
    client, name = server_collection
    await client.upsert(
        name,
        [_point(1, "MR JANE\nDOE"), _point(2, "jane   doe")],
        wait=True,
    )

    results = await ExactMatchAlgorithm().search("Jane Doe", "alice")

    assert {r.id for r in results} == {"1", "2"}


async def test_other_users_documents_are_not_matched(server_collection):
    client, name = server_collection
    await client.upsert(
        name,
        [_point(1, "Jane Doe"), _point(2, "Jane Doe", user_id="bob", owner_id="bob")],
        wait=True,
    )

    results = await ExactMatchAlgorithm().search("Jane Doe", "alice")

    assert [r.id for r in results] == ["1"]
