"""ADR-042 step 4c: image counts in sync status.

``count_images`` is pinned against the in-memory backend here (and against a
real server in tests/integration/test_image_ingest_qdrant_server.py); the two
surfaces are pinned to report it, and to count image-only files as documents.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client import AsyncQdrantClient, models

from nextcloud_mcp_server.api import management
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.server import semantic
from nextcloud_mcp_server.vector import ingest_status, metrics_publisher, payload_keys
from nextcloud_mcp_server.vector import qdrant_client as qdrant_module
from nextcloud_mcp_server.vector.dead_letter import DEAD_LETTER_KEY
from nextcloud_mcp_server.vector.metrics_publisher import ImageCounts, count_images

pytestmark = pytest.mark.unit

IMG = {payload_keys.VECTOR_KIND: payload_keys.VECTOR_KIND_IMAGE}
SKIP = {payload_keys.IMAGE_SKIPPED_REASON: "rejected"}


def image_count_points() -> tuple[list[dict], ImageCounts]:
    """Payloads covering every state, and the counts they must produce."""
    payloads = [
        {"is_placeholder": True, payload_keys.IMAGE_PENDING: True},  # pending
        {"is_placeholder": True},  # a queued PDF: not pending image work
        {**IMG, payload_keys.IMAGE_ONLY: True},  # image-only file
        {**IMG, payload_keys.IMAGE_ONLY: False},  # image + text file ...
        {"chunk_index": 0},  # ... its text
        {"chunk_index": 0, **SKIP},  # skipped, with text (2 chunks, 1 doc)
        {"chunk_index": 1, **SKIP},
        {"is_placeholder": True, DEAD_LETTER_KEY: True, **SKIP},  # textless skip
        {"is_placeholder": True, DEAD_LETTER_KEY: True, "reason": "timeout"},
    ]
    return payloads, ImageCounts(pending=1, embedded=2, image_only=1, skipped=2)


async def test_count_images_counts_one_point_per_document():
    client = AsyncQdrantClient(location=":memory:")
    vec = models.VectorParams(size=2, distance=models.Distance.COSINE)
    await client.create_collection("c", vectors_config={"dense": vec, "image": vec})
    payloads, expected = image_count_points()
    await client.upsert(
        "c",
        points=[
            models.PointStruct(id=i, vector={"dense": [1, 0]}, payload=p)
            for i, p in enumerate(payloads, start=1)
        ],
    )
    assert await count_images(client, "c") == expected
    await client.close()


@pytest.fixture
def counts(monkeypatch):
    """Both surfaces with fixed counts: 3 text documents, 2 image-only files."""
    settings = dataclasses.replace(get_settings(), vector_sync_enabled=True)
    images = ImageCounts(pending=4, embedded=5, image_only=2, skipped=1)
    for module in (semantic, management):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        ingest_status,
        "get_ingest_pending",
        AsyncMock(
            return_value=SimpleNamespace(
                pending=0, job_counts=None, job_counts_by_queue=None
            )
        ),
    )
    for module in (semantic, metrics_publisher):
        monkeypatch.setattr(module, "count_indexed", AsyncMock(return_value=(3, 30)))
        monkeypatch.setattr(module, "count_images", AsyncMock(return_value=images))
        monkeypatch.setattr(
            module, "estimate_hybrid_vector_bytes", AsyncMock(return_value=(30, 0))
        )
    monkeypatch.setattr(semantic, "get_qdrant_client", AsyncMock())
    monkeypatch.setattr(qdrant_module, "get_qdrant_client", AsyncMock())


async def test_tool_reports_image_counts(counts):
    ctx = MagicMock()
    result = await semantic.nc_get_vector_sync_status(ctx)
    assert (result.image_pending, result.image_embedded, result.image_skipped) == (
        4,
        5,
        1,
    )
    assert result.indexed_documents == 3 + 2


async def test_route_reports_image_counts(counts):
    request = MagicMock()
    request.app.state = SimpleNamespace()
    body = json.loads((await management.get_vector_sync_status(request)).body)
    assert (body["image_pending"], body["image_embedded"], body["image_skipped"]) == (
        4,
        5,
        1,
    )
    assert body["indexed_documents"] == 3 + 2
