"""ADR-042 §4 on a real Qdrant server: ``must_not vector_kind == image``.

The unit test pins this on the in-memory client; local mode and the server
have differed on filter semantics before, so the server gets its own check,
with the ``vector_kind`` keyword index the startup migration creates.

Skips when no Qdrant server answers on localhost:6333.
"""

import os
import uuid

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as m

from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.placeholder import exclude_image_points

pytestmark = pytest.mark.integration

_URL = os.getenv("QDRANT_TEST_URL", "http://localhost:6333")
_API_KEY = os.getenv("QDRANT_API_KEY", "my_secret_api_key")


async def test_must_not_keeps_text_points_on_a_server():
    client = AsyncQdrantClient(url=_URL, api_key=_API_KEY, https=False)
    try:
        await client.get_collections()
    except Exception as e:  # noqa: BLE001 - no server: skip, not fail
        await client.close()
        pytest.skip(f"no Qdrant server at {_URL}: {e}")
    name = f"vkind_{uuid.uuid4().hex[:8]}"
    await client.create_collection(
        name,
        vectors_config={"dense": m.VectorParams(size=2, distance=m.Distance.COSINE)},
    )
    try:
        await client.create_payload_index(
            name, payload_keys.VECTOR_KIND, m.PayloadSchemaType.KEYWORD
        )
        await client.upsert(
            name,
            points=[
                m.PointStruct(id=1, vector={"dense": [1.0, 0.0]}, payload={}),
                m.PointStruct(
                    id=2,
                    vector={"dense": [1.0, 0.0]},
                    payload={payload_keys.VECTOR_KIND: payload_keys.VECTOR_KIND_IMAGE},
                ),
            ],
        )
        points, _ = await client.scroll(
            name, scroll_filter=m.Filter(must=[exclude_image_points()])
        )
        assert [p.id for p in points] == [1]
    finally:
        await client.delete_collection(name)
        await client.close()
