"""Tool-layer tests for the WebDAV file-comment tools (GH #1308).

These register the WebDAV tools on a fresh ``MCPServer`` and invoke each tool's
underlying function directly with a mocked client, covering the wiring the
client-layer tests cannot: that the excluded-tag guard and the message
validation run *before* anything is posted, and that a missing file is a clear
refusal rather than a 404 from the comments collection.

Fixture shape mirrors ``tests/unit/test_webdav_tools_exclusion.py``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.server.webdav import configure_webdav_tools
from nextcloud_mcp_server.utils.message_splitter import COMMENT_MAX_LENGTH

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path.

    See test_webdav_tools_exclusion.py — without this the outcome depends on
    the ambient ``MCP_DEPLOYMENT_MODE``.
    """
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def fake_client(mocker):
    """A NextcloudClient-shaped mock, installed as the tools' ``get_client``."""
    client = SimpleNamespace(webdav=AsyncMock())
    client.webdav.get_fileid.return_value = "42"

    async def fake_get_client(_ctx):
        return client

    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", side_effect=fake_get_client
    )
    return client


@pytest.fixture(autouse=True)
def no_excluded_tags(mocker):
    """Default: no tags are excluded. Individual tests override the return."""

    async def fake(*_, **__):
        return set()

    return mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_excluded_file_paths", side_effect=fake
    )


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer(name="test-webdav-comment-tools")
    configure_webdav_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


@pytest.fixture
def create_comment(tools):
    """Resolved outside ``pytest.raises`` blocks so only the call can throw."""
    return tools["nc_webdav_create_comment"].fn


@pytest.fixture
def list_comments(tools):
    return tools["nc_webdav_list_comments"].fn


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace())


async def test_create_comment_posts_and_returns_id(create_comment, fake_client):
    fake_client.webdav.create_comment.return_value = 99

    result = await create_comment("/report.pdf", 'ping @"alice"', _ctx())

    assert (result.file_id, result.comment_id) == (42, 99)
    assert result.message == 'ping @"alice"'
    fake_client.webdav.create_comment.assert_awaited_once_with(42, 'ping @"alice"')


async def test_create_comment_at_the_limit_is_accepted(create_comment, fake_client):
    """Nextcloud's check is a strict ``>``, so exactly the limit is legal."""
    fake_client.webdav.create_comment.return_value = 1

    await create_comment("/report.pdf", "x" * COMMENT_MAX_LENGTH, _ctx())

    fake_client.webdav.create_comment.assert_awaited_once()


@pytest.mark.parametrize(
    "message",
    [
        "",
        "   \n\t ",
        # Non-blank to Python's strip(), but PHP's trim() takes NUL, so the
        # server would store an empty comment and report success.
        "\0",
        # Blank to Python's strip(), non-blank to PHP's: an ideographic space.
        "　",
    ],
)
async def test_create_comment_rejects_blank(create_comment, fake_client, message):
    with pytest.raises(ToolError, match="must not be empty"):
        await create_comment("/report.pdf", message, _ctx())

    fake_client.webdav.create_comment.assert_not_awaited()


async def test_create_comment_rejects_over_length(create_comment, fake_client):
    """The error states the exact overage, so an agent needn't guess how much to cut."""
    with pytest.raises(ToolError, match="5 characters over"):
        await create_comment("/report.pdf", "x" * (COMMENT_MAX_LENGTH + 5), _ctx())

    fake_client.webdav.create_comment.assert_not_awaited()


async def test_create_comment_refuses_excluded_path(
    create_comment, fake_client, no_excluded_tags
):
    no_excluded_tags.side_effect = None
    no_excluded_tags.return_value = {"private"}  # stored without leading slash

    with pytest.raises(ToolError, match="excluded tag"):
        await create_comment("/private/report.pdf", "hi", _ctx())

    fake_client.webdav.create_comment.assert_not_awaited()


async def test_create_comment_on_missing_file(create_comment, fake_client):
    fake_client.webdav.get_fileid.return_value = None

    with pytest.raises(ToolError, match="File not found"):
        await create_comment("/gone.pdf", "hi", _ctx())

    fake_client.webdav.create_comment.assert_not_awaited()


async def test_list_comments_maps_to_models(list_comments, fake_client):
    fake_client.webdav.list_comments.return_value = [
        {
            "id": 7,
            "message": "Please review",
            "actor_id": "bob",
            "actor_type": "users",
            "actor_display_name": "Bob",
            "creation_datetime": "Sat, 15 Aug 2026 08:00:00 GMT",
            "verb": "comment",
            "is_unread": True,
        }
    ]

    result = await list_comments("/report.pdf", _ctx(), limit=5, offset=10)

    assert result.count == 1
    assert result.results[0].actor_display_name == "Bob"
    assert (result.file_id, result.limit, result.offset) == (42, 5, 10)
    fake_client.webdav.list_comments.assert_awaited_once_with(42, limit=5, offset=10)


@pytest.mark.parametrize(
    ("limit", "offset", "match"),
    [(0, 0, "limit must be positive"), (5, -1, "offset must not be negative")],
)
async def test_list_comments_rejects_bad_paging(
    list_comments, fake_client, limit, offset, match
):
    with pytest.raises(ToolError, match=match):
        await list_comments("/report.pdf", _ctx(), limit=limit, offset=offset)

    fake_client.webdav.list_comments.assert_not_awaited()


async def test_list_comments_refuses_excluded_path(
    list_comments, fake_client, no_excluded_tags
):
    no_excluded_tags.side_effect = None
    no_excluded_tags.return_value = {"private"}  # stored without leading slash

    with pytest.raises(ToolError, match="excluded tag"):
        await list_comments("/private/report.pdf", _ctx())

    fake_client.webdav.list_comments.assert_not_awaited()
