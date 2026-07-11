"""Current-corpus chunk-density snapshot against a real Qdrant engine.

Complements the mocked unit tests in
``tests/unit/vector/test_metrics_publisher.py`` by running
``compute_chunk_density_snapshot`` over an in-memory Qdrant collection: this
exercises the actual ``scroll`` pagination, the real ``get_placeholder_filter``
+ ``chunk_index == 0`` filter, and the payload round-trip of
``payload_keys.SOURCE_BYTES`` — none of which the AsyncMock unit tests can
prove.

It also pins the forward-only coverage contract end-to-end: a point written
with ``source_bytes`` lands in the density histogram; a legacy point without it
is reported as uncovered; a placeholder is excluded entirely.
"""

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.observability.metrics import density_bucket_index
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.metrics_publisher import compute_chunk_density_snapshot

pytestmark = pytest.mark.integration

_DIM = 8


def _point(
    pid, *, doc_type, chunk_index, total_chunks, source_bytes=None, is_placeholder=False
):
    payload = {
        "doc_id": str(pid),
        "doc_type": doc_type,
        "is_placeholder": is_placeholder,
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
    }
    if source_bytes is not None:
        payload[payload_keys.SOURCE_BYTES] = source_bytes
    return PointStruct(id=pid, vector={"dense": [0.1] * _DIM}, payload=payload)


@pytest.fixture
async def seeded_collection():
    """In-memory Qdrant seeded with covered / uncovered / placeholder points."""
    client = AsyncQdrantClient(":memory:")
    collection = get_settings().get_collection_name()
    await client.create_collection(
        collection_name=collection,
        vectors_config={"dense": VectorParams(size=_DIM, distance=Distance.COSINE)},
    )
    points = [
        # Covered note: 3 chunks / 1 MB -> density 3.0 (le=5 bucket).
        _point(
            1, doc_type="note", chunk_index=0, total_chunks=3, source_bytes=1_000_000
        ),
        # Covered note: 100 chunks / 1 MB -> density 100.0 (le=120 bucket).
        _point(
            2, doc_type="note", chunk_index=0, total_chunks=100, source_bytes=1_000_000
        ),
        # A non-zero chunk of doc 2 — must be ignored (only chunk_index=0 counts).
        _point(
            3, doc_type="note", chunk_index=1, total_chunks=100, source_bytes=1_000_000
        ),
        # Legacy file: no source_bytes -> uncovered.
        _point(4, doc_type="file", chunk_index=0, total_chunks=10),
        # Placeholder -> excluded entirely (not covered, not uncovered).
        _point(
            5,
            doc_type="note",
            chunk_index=0,
            total_chunks=1,
            source_bytes=1_000_000,
            is_placeholder=True,
        ),
    ]
    await client.upsert(collection_name=collection, points=points, wait=True)
    yield client, collection
    await client.close()


async def test_snapshot_covers_uncovered_and_excludes_placeholder(seeded_collection):
    client, collection = seeded_collection

    per_doc_type, uncovered, truncated = await compute_chunk_density_snapshot(
        client, collection, max_documents=1000
    )

    assert truncated is False
    # Placeholder excluded; legacy file counted as uncovered.
    assert uncovered == {"file": 1}

    # Only the two chunk_index=0 covered notes contribute (the chunk_index=1
    # point and the placeholder are ignored).
    note_counts, note_gsum = per_doc_type["note"]
    assert sum(note_counts) == 2
    assert note_counts[density_bucket_index(3.0)] == 1
    assert note_counts[density_bucket_index(100.0)] == 1
    assert note_gsum == pytest.approx(103.0)
    # File produced no covered docs, so it has no density series.
    assert "file" not in per_doc_type


async def test_truncation_signalled_against_real_engine(seeded_collection):
    client, collection = seeded_collection

    # Cap below the covered-document count with a tiny page size so the scroll
    # has a next page when the cap is reached.
    _, _, truncated = await compute_chunk_density_snapshot(
        client, collection, max_documents=1, page_size=1
    )

    assert truncated is True
