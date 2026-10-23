"""End-to-end tests for ``nc_webdav_edit_file`` and line-range reads.

Each test creates its own file directly over WebDAV, drives the MCP tools
against it, and checks the result by reading the file back directly, so the
assertions do not depend on the tools under test.
"""

import json
import uuid

import pytest

pytestmark = [pytest.mark.integration]


def _payload(result) -> dict:
    assert result.is_error is False, f"tool failed: {result.content}"
    return json.loads(result.content[0].text)


def _error_text(result) -> str:
    assert result.is_error is True, f"expected an error, got: {result.content}"
    return result.content[0].text


@pytest.fixture
async def text_file(nc_client):
    """Create ``mcp_edit_<uuid>.md`` with given bytes; delete it afterwards."""
    created: list[str] = []

    async def _create(body: bytes, suffix: str = ".md") -> str:
        path = f"mcp_edit_{uuid.uuid4().hex[:8]}{suffix}"
        await nc_client.webdav.write_file(path, body, "text/markdown")
        created.append(path)
        return path

    yield _create

    for path in created:
        try:
            await nc_client.webdav.delete_resource(path)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


async def _content(nc_client, path: str) -> bytes:
    body, _, _ = await nc_client.webdav.read_file(path)
    return body


async def test_edit_changes_only_the_quoted_passage(
    nc_mcp_client, nc_client, text_file
):
    path = await text_file(b"# Plan\n\n- step one\n- step two\n")

    data = _payload(
        await nc_mcp_client.call_tool(
            "nc_webdav_edit_file",
            {
                "path": path,
                "edits": [{"old_string": "- step two", "new_string": "- step 2"}],
            },
        )
    )

    assert await _content(nc_client, path) == b"# Plan\n\n- step one\n- step 2\n"
    assert data["replacements"] == 1
    assert "+- step 2" in data["diff"].splitlines()
    # The returned etag is the file's current one: usable as the next if_match.
    _, _, etag = await nc_client.webdav.read_file(path)
    assert data["etag"] == etag


async def test_several_edits_in_one_call(nc_mcp_client, nc_client, text_file):
    path = await text_file(b"alpha\nbeta\ngamma\n")

    data = _payload(
        await nc_mcp_client.call_tool(
            "nc_webdav_edit_file",
            {
                "path": path,
                "edits": [
                    {"old_string": "alpha", "new_string": "ALPHA"},
                    {"old_string": "gamma", "new_string": "GAMMA"},
                ],
            },
        )
    )

    assert await _content(nc_client, path) == b"ALPHA\nbeta\nGAMMA\n"
    assert data["edits_applied"] == 2


async def test_ambiguous_edit_leaves_the_file_unchanged(
    nc_mcp_client, nc_client, text_file
):
    original = b"todo\nother\ntodo\n"
    path = await text_file(original)

    text = _error_text(
        await nc_mcp_client.call_tool(
            "nc_webdav_edit_file",
            {"path": path, "edits": [{"old_string": "todo", "new_string": "done"}]},
        )
    )

    assert "occurs 2 times" in text
    assert await _content(nc_client, path) == original


async def test_stale_if_match_leaves_the_file_unchanged(
    nc_mcp_client, nc_client, text_file
):
    """Someone changed the file after the agent read it: the edit is refused."""
    path = await text_file(b"version one\n")
    _, _, stale_etag = await nc_client.webdav.read_file(path)
    await nc_client.webdav.write_file(
        path, b"version two\n", "text/markdown", if_match=stale_etag
    )

    text = _error_text(
        await nc_mcp_client.call_tool(
            "nc_webdav_edit_file",
            {
                "path": path,
                "edits": [{"old_string": "version", "new_string": "release"}],
                "if_match": stale_etag,
            },
        )
    )

    assert "changed since you read it" in text
    assert await _content(nc_client, path) == b"version two\n"


async def test_crlf_line_endings_survive_an_edit(nc_mcp_client, nc_client, text_file):
    path = await text_file(b"first\r\nsecond\r\nthird\r\n", suffix=".txt")

    _payload(
        await nc_mcp_client.call_tool(
            "nc_webdav_edit_file",
            {
                "path": path,
                "edits": [{"old_string": "first\nsecond", "new_string": "1st\n2nd"}],
            },
        )
    )

    assert await _content(nc_client, path) == b"1st\r\n2nd\r\nthird\r\n"


async def test_read_line_range(nc_mcp_client, text_file):
    path = await text_file(b"l1\nl2\nl3\nl4\nl5\n", suffix=".txt")

    data = _payload(
        await nc_mcp_client.call_tool(
            "nc_webdav_read_file",
            {"path": path, "line_start": 2, "line_end": 3, "line_numbers": True},
        )
    )

    assert data["content"] == "     2\tl2\n     3\tl3"
    assert (data["line_start"], data["line_end"], data["total_lines"]) == (2, 3, 5)
    assert data["etag"]
