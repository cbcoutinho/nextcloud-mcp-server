"""ADR-042 step 3 against a real Qdrant server (the compose ``qdrant`` profile).

The unit tests cover the in-memory client. This proves the in-place
``create_vector_name`` on a server (>= 1.18): an existing text-only collection
gains the ``image`` vector without touching its points, and image-only points
can then be written and searched next to them.

Skips when no Qdrant server answers on localhost:6333.
"""

import os
import uuid

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as m

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.providers.simple import SIMPLE_IMAGE_MODEL, SimpleProvider
from nextcloud_mcp_server.vector import qdrant_client as qc

pytestmark = pytest.mark.integration

_URL = os.getenv("QDRANT_TEST_URL", "http://localhost:6333")
_API_KEY = os.getenv("QDRANT_API_KEY", "my_secret_api_key")
_WIDTH = 8


@pytest.fixture
async def text_only_collection():
    client = AsyncQdrantClient(url=_URL, api_key=_API_KEY, https=False)
    try:
        await client.get_collections()
    except Exception as e:  # noqa: BLE001 - no server: skip, not fail
        await client.close()
        pytest.skip(f"no Qdrant server at {_URL}: {e}")
    name = f"image_{uuid.uuid4().hex[:8]}"
    await client.create_collection(
        name,
        vectors_config={"dense": m.VectorParams(size=2, distance=m.Distance.COSINE)},
        sparse_vectors_config={"sparse": m.SparseVectorParams()},
    )
    await client.upsert(
        name,
        points=[m.PointStruct(id=1, vector={"dense": [1.0, 0.0]}, payload={"t": 1})],
    )
    try:
        yield client, name
    finally:
        await client.delete_collection(name)
        await client.close()


async def test_existing_collection_gains_the_image_vector(
    text_only_collection, monkeypatch
):
    client, name = text_only_collection
    settings = Settings(
        image_embedding_model=SIMPLE_IMAGE_MODEL, image_search_score_threshold=0.0
    )
    provider = SimpleProvider(
        dimension=_WIDTH, image_embedding_model=SIMPLE_IMAGE_MODEL
    )
    monkeypatch.setattr(qc, "get_settings", lambda: settings)
    monkeypatch.setattr(qc, "get_provider", lambda: provider)

    vectors = (await client.get_collection(name)).config.params.vectors
    assert await qc._ensure_image_vector(client, name, vectors) is True

    vectors = (await client.get_collection(name)).config.params.vectors
    assert vectors["image"].size == _WIDTH
    assert vectors["dense"].size == 2
    # The existing text point is untouched: no re-index on enable.
    [old] = await client.retrieve(name, ids=[1], with_vectors=True)
    assert old.vector == {"dense": [1.0, 0.0]}

    # An image-only point (ADR-042 §4) is writable and searchable by "image".
    image_vector = [1.0] + [0.0] * (_WIDTH - 1)
    await client.upsert(
        name, points=[m.PointStruct(id=2, vector={"image": image_vector})]
    )
    hits = await client.query_points(name, query=image_vector, using="image", limit=5)
    assert [p.id for p in hits.points] == [2]

    # A restart finds the vector already there and stays ready.
    vectors = (await client.get_collection(name)).config.params.vectors
    assert await qc._ensure_image_vector(client, name, vectors) is True
