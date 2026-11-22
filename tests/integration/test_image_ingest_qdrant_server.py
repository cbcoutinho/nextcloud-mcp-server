"""ADR-042 step 4 on a real Qdrant server: prune never reaches the image point.

``prune_stale_chunks`` deletes ``chunk_index >= kept`` with a range filter. A
range filter must not match a point that lacks the field, so a document's
image point survives every re-index, while a whole-document delete (by
``doc_id``/``doc_type``) still removes it. The unit test pins this in local
mode; the two backends have differed on filters before.

Skips when no Qdrant server answers on localhost:6333.
"""

import os
import uuid

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as m

from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.processor import prune_stale_chunks

pytestmark = pytest.mark.integration

_URL = os.getenv("QDRANT_TEST_URL", "http://localhost:6333")
_API_KEY = os.getenv("QDRANT_API_KEY", "my_secret_api_key")


async def test_prune_spares_the_image_point_on_a_server():
    client = AsyncQdrantClient(url=_URL, api_key=_API_KEY, https=False)
    try:
        await client.get_collections()
    except Exception as e:  # noqa: BLE001 - no server: skip, not fail
        await client.close()
        pytest.skip(f"no Qdrant server at {_URL}: {e}")
    name = f"imgprune_{uuid.uuid4().hex[:8]}"
    vec = m.VectorParams(size=2, distance=m.Distance.COSINE)
    await client.create_collection(name, vectors_config={"dense": vec, "image": vec})
    try:
        # The indexes the startup migration creates; strict mode needs them.
        for key, schema in (
            ("doc_id", m.PayloadSchemaType.KEYWORD),
            ("doc_type", m.PayloadSchemaType.KEYWORD),
            ("chunk_index", m.PayloadSchemaType.INTEGER),
        ):
            await client.create_payload_index(name, key, schema)
        doc = {"doc_id": "7", "doc_type": "file"}
        image_id = payload_keys.image_point_id("7")
        await client.upsert(
            name,
            points=[
                m.PointStruct(
                    id=1, vector={"dense": [1, 0]}, payload={**doc, "chunk_index": 0}
                ),
                m.PointStruct(
                    id=2, vector={"dense": [1, 0]}, payload={**doc, "chunk_index": 1}
                ),
                m.PointStruct(
                    id=image_id,
                    vector={"image": [0, 1]},
                    payload={
                        **doc,
                        payload_keys.VECTOR_KIND: payload_keys.VECTOR_KIND_IMAGE,
                    },
                ),
            ],
            wait=True,
        )

        await prune_stale_chunks(
            client, collection_name=name, doc_id="7", doc_type="file", kept_chunks=1
        )
        points, _ = await client.scroll(name, limit=10)
        assert {str(p.id) for p in points} == {"1", image_id}

        await client.delete(
            name,
            points_selector=m.Filter(
                must=[
                    m.FieldCondition(key=k, match=m.MatchValue(value=v))
                    for k, v in doc.items()
                ]
            ),
            wait=True,
        )
        assert (await client.count(name)).count == 0
    finally:
        await client.delete_collection(name)
        await client.close()
