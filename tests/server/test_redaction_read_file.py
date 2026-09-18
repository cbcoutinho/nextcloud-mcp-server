"""End-to-end: redacted ``nc_webdav_read_file`` against the running stack (ADR-038).

The ``mcp`` compose service points ``EMBEDDING_GATEWAY_URL`` at the ``ner-stub``
service, which "detects" a fixed set of synthetic names
(``tests/fixtures/ner_stub.py``). So this exercises the real path — MCP call,
WebDAV download, NER over HTTP, redaction — with a deterministic detector.
"""

import json
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

pytestmark = pytest.mark.integration


async def test_read_file_redacts_third_parties_and_keeps_subject(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    folder = f"redaction-{uuid.uuid4().hex[:8]}"
    path = f"{folder}/Karen Smith notes.txt"
    await nc_client.webdav.create_directory(folder)
    try:
        await nc_client.webdav.write_file(
            path,
            b"Jane Doe met Karen Smith and Tom Brown.",
            content_type="text/plain",
        )

        result = await nc_mcp_client.call_tool(
            "nc_webdav_read_file",
            {"path": path, "redact": True, "keep_names": ["Jane Doe"]},
        )

        assert result.is_error is False, result.content
        data = json.loads(result.content[0].text)
        assert data["content"] == "Jane Doe met [PERSON_1] and [PERSON_2]."
        assert data["path"] == f"{folder}/[PERSON_1] notes.txt"
        assert data["redaction"]["applied"] is True
        assert data["redaction"]["persons_redacted"] == 2
        assert data["redaction"]["kept_names"] == ["Jane Doe"]

        # Without redact the same read is untouched.
        plain = await nc_mcp_client.call_tool("nc_webdav_read_file", {"path": path})
        plain_data = json.loads(plain.content[0].text)
        assert plain_data["content"] == "Jane Doe met Karen Smith and Tom Brown."
        assert plain_data["redaction"] is None
    finally:
        await nc_client.webdav.delete_resource(folder)
