"""The local-mode scan snapshot answers exactly like the per-file scrolls (GH #1941).

Property: for any set of file points in a real in-memory Qdrant, every lookup
the tagged-file loop makes (dedup claim, dead-letter check, user metadata)
returns the same result from the one-pass snapshot as from its filtered scroll.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qdrant_client import AsyncQdrantClient, models

from nextcloud_mcp_server.vector import dead_letter, placeholder, scanner, sharing_state
from nextcloud_mcp_server.vector.scan_snapshot import IndexSnapshot
from nextcloud_mcp_server.vector.scanner import _iter_all_points

pytestmark = pytest.mark.unit

COLLECTION = "c"
DOC_IDS = ["1", "2", "3"]
ETAGS = ["e1", "e2"]
USERS = ["alice", "bob"]
IDENTITY = "model-a"

payloads = st.fixed_dictionaries(
    {
        # 3 (an int) is a legacy doc_id that a string MatchValue never matches.
        "doc_id": st.sampled_from([*DOC_IDS, 3]),
        "doc_type": st.sampled_from(["file", "file", "note"]),
        "is_placeholder": st.booleans(),
        "etag": st.sampled_from(ETAGS),
        "modified_at": st.integers(0, 3),
        "embedding_identity": st.sampled_from([IDENTITY, "model-b"]),
    },
    optional={
        "user_id": st.sampled_from(USERS),
        "dead_letter": st.booleans(),
        "tiers_sig": st.sampled_from(["t1", "t2"]),
        "excerpt": st.text(max_size=5),
    },
)


@settings(max_examples=60)
@given(points=st.lists(payloads, max_size=12))
async def test_snapshot_matches_per_file_scrolls(points):
    with pytest.MonkeyPatch.context() as monkeypatch:
        await _check(points, monkeypatch)


async def _check(points, monkeypatch):
    client = AsyncQdrantClient(":memory:")
    await client.create_collection(
        COLLECTION,
        vectors_config={
            "dense": models.VectorParams(size=1, distance=models.Distance.COSINE)
        },
    )
    if points:
        await client.upsert(
            COLLECTION,
            points=[
                models.PointStruct(id=i + 1, vector={"dense": [1.0]}, payload=p)
                for i, p in enumerate(points)
            ],
        )

    async def get_client():
        return client

    fake_settings = SimpleNamespace(get_collection_name=lambda: COLLECTION)
    for module in (dead_letter, placeholder, sharing_state):
        monkeypatch.setattr(module, "get_qdrant_client", get_client)
        monkeypatch.setattr(module, "get_settings", lambda: fake_settings)

    snapshot = await IndexSnapshot.collect(
        _iter_all_points(
            client,
            collection_name=COLLECTION,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="doc_type", match=models.MatchValue(value="file")
                    )
                ]
            ),
            payload_fields=models.PayloadSelectorExclude(exclude=["excerpt"]),
        ),
        set(DOC_IDS),
    )

    def strip(payload):
        return (
            None
            if payload is None
            else {k: v for k, v in payload.items() if k != "excerpt"}
        )

    for doc_id in DOC_IDS:
        for etag in ETAGS:
            assert strip(
                await sharing_state.find_indexed_content(doc_id, "file", etag, IDENTITY)
            ) == await sharing_state.find_indexed_content(
                doc_id, "file", etag, IDENTITY, snapshot=snapshot
            )
            for tiers_sig in ("t1", "t2"):
                assert await dead_letter.is_dead_lettered(
                    doc_id, "file", etag, tiers_sig
                ) == await dead_letter.is_dead_lettered(
                    doc_id, "file", etag, tiers_sig, snapshot=snapshot
                )
        for user_id in USERS:
            assert strip(
                await placeholder.query_document_metadata(doc_id, "file", user_id)
            ) == await placeholder.query_document_metadata(
                doc_id, "file", user_id, snapshot=snapshot
            )
    await client.close()


async def test_snapshot_only_in_local_mode_and_fails_open(monkeypatch):
    """Network mode skips the snapshot; a failed read falls back to None."""
    network = SimpleNamespace(qdrant_location=None)
    assert await scanner._local_mode_file_snapshot(network, {"1"}) is None

    async def broken():
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(scanner, "get_qdrant_client", broken)
    local = SimpleNamespace(
        qdrant_location=":memory:", get_collection_name=lambda: COLLECTION
    )
    assert await scanner._local_mode_file_snapshot(local, {"1"}) is None
