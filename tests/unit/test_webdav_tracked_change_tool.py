"""Tool-layer tests for ``nc_webdav_insert_tracked_change``.

The OOXML rewriting itself is covered by ``test_docx_revisions.py``; these pin
the wiring around it: the excluded-tag guard runs before any read, the
write-back is conditional on the etag of that read, and every refusal surfaces
as a ``ToolError`` with nothing written.

Fixture shape mirrors ``tests/unit/test_webdav_comment_tools.py``.
"""

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import docx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.server.webdav import (
    DOCX_CONTENT_TYPE,
    configure_webdav_tools,
)

pytestmark = pytest.mark.unit


def _docx(text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path."""
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture(autouse=True)
def write_cap(mocker):
    settings = SimpleNamespace(webdav_write_max_mb=10)
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings", return_value=settings
    )
    return settings


@pytest.fixture
def fake_client(mocker):
    client = SimpleNamespace(webdav=AsyncMock(), username="bob")
    client.webdav.read_file.return_value = (
        _docx("The quick fox."),
        DOCX_CONTENT_TYPE,
        "etag-1",
    )
    client.webdav.write_file.return_value = {"status_code": 204, "etag": "etag-2"}

    async def fake_get_client(_ctx):
        return client

    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", side_effect=fake_get_client
    )
    return client


@pytest.fixture(autouse=True)
def no_excluded_tags(mocker):
    async def fake(*_, **__):
        return set()

    return mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_excluded_file_paths", side_effect=fake
    )


@pytest.fixture
def tracked_change():
    mcp = MCPServer(name="test-webdav-tracked-change")
    configure_webdav_tools(mcp)
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    return tools["nc_webdav_insert_tracked_change"].fn


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace())


def _written_document_xml(fake_client) -> str:
    content = fake_client.webdav.write_file.await_args.args[1]
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return archive.read("word/document.xml").decode()


async def test_writes_back_conditionally_on_the_read_etag(tracked_change, fake_client):
    result = await tracked_change(
        "/Docs/a.docx", "quick", _ctx(), new_text=" brown", author="alice"
    )

    args = fake_client.webdav.write_file.await_args
    assert args.args[0] == "/Docs/a.docx"
    assert args.args[2] == DOCX_CONTENT_TYPE
    assert args.kwargs == {"if_match": "etag-1"}
    assert 'w:author="alice"' in _written_document_xml(fake_client)
    assert result.etag == "etag-2"
    assert result.author == "alice"
    assert result.mode == "insert"
    assert result.paragraph_text == "The quick fox."
    assert result.size == len(args.args[1])


async def test_author_defaults_to_the_nextcloud_user(tracked_change, fake_client):
    result = await tracked_change("/a.docx", "quick", _ctx(), new_text="!")

    assert result.author == "bob"
    assert 'w:author="bob"' in _written_document_xml(fake_client)


async def test_concurrent_edit_surfaces_as_tool_error(tracked_change, fake_client):
    fake_client.webdav.write_file.return_value = {
        "status_code": 412,
        "message": "File changed since it was read",
    }

    with pytest.raises(ToolError, match="changed since it was read"):
        await tracked_change("/a.docx", "quick", _ctx(), new_text="!")


async def test_locked_file_surfaces_as_tool_error(tracked_change, fake_client):
    fake_client.webdav.write_file.return_value = {
        "status_code": 423,
        "message": "File is locked",
    }

    with pytest.raises(ToolError, match="locked"):
        await tracked_change("/a.docx", "quick", _ctx(), new_text="!")


async def test_unmatched_anchor_writes_nothing(tracked_change, fake_client):
    with pytest.raises(ToolError, match="not found"):
        await tracked_change("/a.docx", "absent", _ctx(), new_text="!")

    fake_client.webdav.write_file.assert_not_awaited()


async def test_missing_etag_is_refused_rather_than_force_written(
    tracked_change, fake_client
):
    fake_client.webdav.read_file.return_value = (
        _docx("The quick fox."),
        DOCX_CONTENT_TYPE,
        None,
    )

    with pytest.raises(ToolError, match="no ETag"):
        await tracked_change("/a.docx", "quick", _ctx(), new_text="!")

    fake_client.webdav.write_file.assert_not_awaited()


async def test_file_over_the_write_cap_is_refused(
    tracked_change, fake_client, write_cap
):
    write_cap.webdav_write_max_mb = 0.000001

    with pytest.raises(ToolError, match="WEBDAV_WRITE_MAX_MB"):
        await tracked_change("/a.docx", "quick", _ctx(), new_text="!")

    fake_client.webdav.write_file.assert_not_awaited()


async def test_excluded_path_is_refused_before_reading(
    tracked_change, fake_client, no_excluded_tags
):
    no_excluded_tags.side_effect = None
    no_excluded_tags.return_value = {"private"}

    with pytest.raises(ToolError, match="excluded tag"):
        await tracked_change("/private/a.docx", "quick", _ctx(), new_text="!")

    fake_client.webdav.read_file.assert_not_awaited()
    fake_client.webdav.write_file.assert_not_awaited()
