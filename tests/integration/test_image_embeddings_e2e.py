"""ADR-042 end to end on the single-user stack: a tagged PNG gets an image point.

The ``mcp`` container runs image embeddings on the Simple provider's test-only
``simple-image-hash`` model (docker-compose.yml), so this drives the real path:
discovery of a type no text processor reads, the worker's image embedding, and
the image counts on both status surfaces.
"""

import io
import json
import logging
import time
import uuid

import anyio
import httpx2
import pytest
from mcp import ClientSession
from nextcloud_client import NextcloudClient
from PIL import Image

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration

_STATUS_URL = "http://localhost:8000/api/v1/vector-sync/status"
_IMAGE_KEYS = ("image_pending", "image_embedded", "image_skipped")


def _png() -> bytes:
    buf = io.BytesIO()
    # Unique pixels per run: the dedup key is the file, but a distinct image
    # keeps runs from looking alike in a shared dev stack.
    Image.new("RGB", (32, 32), tuple(uuid.uuid4().bytes[:3])).save(buf, format="PNG")
    return buf.getvalue()


async def _tool_status(client: ClientSession) -> dict:
    result = await client.call_tool("nc_get_vector_sync_status", {})
    assert not result.is_error, result
    return json.loads(result.content[0].text)


@pytest.fixture
async def tagged_png(nc_client: NextcloudClient):
    test_dir = f"mcp_image_e2e_{uuid.uuid4().hex[:8]}"
    path = f"{test_dir}/photo.png"
    await nc_client.webdav.create_directory(test_dir)
    await nc_client.webdav.write_file(path, _png(), "image/png")
    tag = await nc_client.webdav.get_or_create_tag(
        name="vector-index", user_visible=True, user_assignable=True
    )
    info = await nc_client.webdav.get_file_info(path)
    await nc_client.webdav.assign_tag_to_file(info["id"], tag["id"])
    yield {"path": path, "file_id": str(info["id"])}
    try:
        await nc_client.webdav.delete_resource(test_dir)
    except Exception as e:  # noqa: BLE001 - best-effort cleanup
        logger.warning("failed to delete %s: %s", test_dir, e)


async def test_both_status_surfaces_report_image_counts(nc_mcp_client):
    status = await _tool_status(nc_mcp_client)
    async with httpx2.AsyncClient() as http:
        body = (await http.get(_STATUS_URL)).json()
    for key in _IMAGE_KEYS:
        assert isinstance(status[key], int), key
        assert isinstance(body[key], int), key


async def test_tagged_png_is_embedded(nc_mcp_client, tagged_png):
    before = (await _tool_status(nc_mcp_client))["image_embedded"]
    deadline = time.monotonic() + 150  # a scan interval plus worker time
    status: dict = {}
    while time.monotonic() < deadline:
        status = await _tool_status(nc_mcp_client)
        if status["image_embedded"] > before:
            break
        await anyio.sleep(5)
    assert status["image_embedded"] > before, (
        f"the tagged PNG never got an image point: {status}"
    )
    # Image-only files count as documents (they have no chunk_index 0 point).
    assert status["indexed_documents"] >= 1
