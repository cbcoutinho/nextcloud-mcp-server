"""End-to-end integration test for two-tag index-mode discovery (ADR-031).

The scanner discovers files under two Nextcloud system tags —
``VECTOR_SYNC_TAG`` (hybrid) and ``VECTOR_SYNC_KEYWORD_TAG`` (keyword-only)
— via ``_discover_tagged_files``, which stamps each file with ``_index_mode``
and applies **hybrid precedence** (a file carrying both tags is hybrid). This
exercises the real OCS Tags API for both tags against a running Nextcloud, which
the mocked unit tests in ``tests/unit/vector/test_index_mode_discovery.py``
cannot.
"""

import logging
import uuid
from types import SimpleNamespace

import pytest

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.scanner import _discover_tagged_files

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


@pytest.fixture
async def dual_tag_environment(nc_client: NextcloudClient):
    """Provision a hybrid tag, a keyword tag, and three PDFs: hybrid-only,
    keyword-only, and one carrying both tags."""
    suffix = uuid.uuid4().hex[:8]
    hybrid_tag = f"mcp-hybrid-{suffix}"
    keyword_tag = f"mcp-keyword-{suffix}"
    test_dir = f"mcp_idx_mode_{suffix}"
    hybrid_pdf = f"{test_dir}/hybrid.pdf"
    keyword_pdf = f"{test_dir}/keyword.pdf"
    both_pdf = f"{test_dir}/both.pdf"

    # Minimal valid PDF bytes so Nextcloud reports application/pdf.
    pdf_bytes = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"
    )

    await nc_client.webdav.create_directory(test_dir)
    for path in (hybrid_pdf, keyword_pdf, both_pdf):
        await nc_client.webdav.write_file(path, pdf_bytes, "application/pdf")

    hybrid = await nc_client.webdav.get_or_create_tag(
        name=hybrid_tag, user_visible=True, user_assignable=True
    )
    keyword = await nc_client.webdav.get_or_create_tag(
        name=keyword_tag, user_visible=True, user_assignable=True
    )

    ids = {}
    for key, path in (
        ("hybrid", hybrid_pdf),
        ("keyword", keyword_pdf),
        ("both", both_pdf),
    ):
        info = await nc_client.webdav.get_file_info(path)
        assert info is not None
        ids[key] = info["id"]

    await nc_client.webdav.assign_tag_to_file(ids["hybrid"], hybrid["id"])
    await nc_client.webdav.assign_tag_to_file(ids["keyword"], keyword["id"])
    await nc_client.webdav.assign_tag_to_file(ids["both"], hybrid["id"])
    await nc_client.webdav.assign_tag_to_file(ids["both"], keyword["id"])

    yield {
        "hybrid_tag": hybrid_tag,
        "keyword_tag": keyword_tag,
        "ids": {str(k): str(v) for k, v in ids.items()},
        "settings": SimpleNamespace(
            vector_sync_tag=hybrid_tag,
            vector_sync_keyword_tag=keyword_tag,
        ),
    }

    try:
        await nc_client.webdav.delete_resource(test_dir)
    except Exception as e:
        logger.warning("failed to delete %s: %s", test_dir, e)


async def test_discover_stamps_modes_with_hybrid_precedence(
    dual_tag_environment, nc_client: NextcloudClient
):
    """Hybrid-only → hybrid, keyword-only → keyword, both-tags → hybrid (once)."""
    env = dual_tag_environment

    files = await _discover_tagged_files(nc_client, env["settings"])

    by_id = {str(f["id"]): f["_index_mode"] for f in files}
    ids = env["ids"]
    assert by_id.get(ids["hybrid"]) == payload_keys.INDEX_MODE_HYBRID
    assert by_id.get(ids["keyword"]) == payload_keys.INDEX_MODE_KEYWORD
    # A file carrying both tags is hybrid (superset) and appears exactly once.
    assert by_id.get(ids["both"]) == payload_keys.INDEX_MODE_HYBRID
    returned_both = [f for f in files if str(f["id"]) == ids["both"]]
    assert len(returned_both) == 1
