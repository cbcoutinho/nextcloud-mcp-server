"""Server-layer regression tests for tag-based file exclusion (issue #710).

These tests register the WebDAV tools on a fresh ``FastMCP`` instance and
invoke each tool's underlying function directly via the tool registry.
Their purpose is **not** to re-test the path-matching logic (covered in
``test_tag_exclusion.py``) but to catch wiring regressions: that each
tool actually consults ``get_excluded_file_paths`` / ``is_path_excluded``
at the right point and raises / filters as expected.

The decorators on each tool (``@require_scopes``, ``@instrument_tool``)
are transparent under our mocked ``Context`` (no ``access_token`` set →
BasicAuth pass-through path).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from nextcloud_mcp_server.server.webdav import configure_webdav_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def webdav_tools() -> dict:
    """Register the WebDAV tools on a fresh FastMCP and return them by name."""
    mcp = FastMCP(name="test-webdav-tools")
    configure_webdav_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


def _mock_ctx(client) -> SimpleNamespace:
    """Build a minimal Context-shaped object for the tool decorators.

    Setting ``request_context.access_token = None`` causes ``require_scopes``
    to take the BasicAuth pass-through branch (see scope_authorization.py).
    """
    ctx = SimpleNamespace()
    ctx.request_context = SimpleNamespace(access_token=None)
    ctx._client = client  # only used by tools that fetch via get_client(ctx)
    return ctx


@pytest.fixture
def patch_get_client(mocker):
    """Replace ``get_client`` in the webdav server module with a mock."""

    def _install(client):
        async def fake_get_client(ctx):
            return client

        mocker.patch(
            "nextcloud_mcp_server.server.webdav.get_client",
            side_effect=fake_get_client,
        )

    return _install


@pytest.fixture
def patch_excluded(mocker):
    """Replace ``get_excluded_file_paths`` with a fixed return value."""

    def _install(excluded: set[str]):
        async def fake(*_, **__):
            return excluded

        mocker.patch(
            "nextcloud_mcp_server.server.webdav.get_excluded_file_paths",
            side_effect=fake,
        )

    return _install


@pytest.fixture
def fake_client():
    """A NextcloudClient-shaped mock with an AsyncMock webdav attribute."""
    client = SimpleNamespace()
    client.webdav = AsyncMock()
    return client


# ── Read / mutate guards ────────────────────────────────────────────────


async def test_read_file_raises_when_path_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})

    fn = webdav_tools["nc_webdav_read_file"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(path="/Secret.txt", ctx=_mock_ctx(fake_client))

    fake_client.webdav.read_file.assert_not_called()


async def test_read_file_passes_through_when_not_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})
    fake_client.webdav.read_file = AsyncMock(return_value=(b"hello", "text/plain"))

    fn = webdav_tools["nc_webdav_read_file"].fn
    result = await fn(path="/Public/notes.md", ctx=_mock_ctx(fake_client))

    assert result["content"] == "hello"
    fake_client.webdav.read_file.assert_awaited_once_with("/Public/notes.md")


async def test_write_file_raises_when_path_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_write_file"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(
            path="/Private/note.md",
            content="hi",
            ctx=_mock_ctx(fake_client),
        )

    fake_client.webdav.write_file.assert_not_called()


async def test_delete_resource_raises_when_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})

    fn = webdav_tools["nc_webdav_delete_resource"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(path="/Secret.txt", ctx=_mock_ctx(fake_client))

    fake_client.webdav.delete_resource.assert_not_called()


async def test_create_directory_raises_when_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_create_directory"].fn
    with pytest.raises(ToolError, match="is or is inside"):
        await fn(path="/Private/sub", ctx=_mock_ctx(fake_client))

    fake_client.webdav.create_directory.assert_not_called()


async def test_move_resource_blocks_excluded_source(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})

    fn = webdav_tools["nc_webdav_move_resource"].fn
    with pytest.raises(ToolError, match="source"):
        await fn(
            source_path="/Secret.txt",
            destination_path="/Public/x.txt",
            ctx=_mock_ctx(fake_client),
        )

    fake_client.webdav.move_resource.assert_not_called()


async def test_move_resource_blocks_excluded_destination_exact_match(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    """Destination check must trip on an *exact* match, not just a prefix.

    Regression guard for review #764: previously the message said "is
    inside" but is_path_excluded also matches exact paths.
    """
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_move_resource"].fn
    with pytest.raises(ToolError, match="is or is inside"):
        await fn(
            source_path="/Public/x.txt",
            destination_path="/Private",
            ctx=_mock_ctx(fake_client),
        )


async def test_copy_resource_blocks_excluded_source(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})

    fn = webdav_tools["nc_webdav_copy_resource"].fn
    with pytest.raises(ToolError, match="source"):
        await fn(
            source_path="/Secret.txt",
            destination_path="/Public/copy.txt",
            ctx=_mock_ctx(fake_client),
        )

    fake_client.webdav.copy_resource.assert_not_called()


async def test_copy_resource_blocks_excluded_destination_descendant(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_copy_resource"].fn
    with pytest.raises(ToolError, match="is or is inside"):
        await fn(
            source_path="/Public/x.txt",
            destination_path="/Private/copy.txt",
            ctx=_mock_ctx(fake_client),
        )


# ── Listing / search filtering ──────────────────────────────────────────


async def test_list_directory_filters_excluded_children(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Public/Secret.txt"})
    fake_client.webdav.list_directory = AsyncMock(
        return_value=[
            {
                "path": "/Public/Secret.txt",
                "name": "Secret.txt",
                "is_directory": False,
            },
            {
                "path": "/Public/visible.md",
                "name": "visible.md",
                "is_directory": False,
            },
        ]
    )

    fn = webdav_tools["nc_webdav_list_directory"].fn
    result = await fn(path="/Public", ctx=_mock_ctx(fake_client))

    assert [f.path for f in result.files] == ["/Public/visible.md"]


async def test_list_directory_raises_when_listed_path_itself_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    """The early guard prevents the round-trip to Nextcloud and signals
    the access denial, instead of silently returning an empty listing
    (review #764)."""
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_list_directory"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(path="/Private", ctx=_mock_ctx(fake_client))

    fake_client.webdav.list_directory.assert_not_called()


async def test_search_files_filters_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})
    fake_client.webdav.search_files = AsyncMock(
        return_value=[
            {"path": "/Secret.txt", "name": "Secret.txt", "is_directory": False},
            {"path": "/notes.md", "name": "notes.md", "is_directory": False},
        ]
    )

    fn = webdav_tools["nc_webdav_search_files"].fn
    result = await fn(ctx=_mock_ctx(fake_client), name_pattern="%.%")

    assert [r.path for r in result.results] == ["/notes.md"]


async def test_find_by_name_filters_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})
    fake_client.webdav.find_by_name = AsyncMock(
        return_value=[
            {"path": "/Secret.txt", "name": "Secret.txt", "is_directory": False},
            {"path": "/visible.txt", "name": "visible.txt", "is_directory": False},
        ]
    )

    fn = webdav_tools["nc_webdav_find_by_name"].fn
    result = await fn(pattern="%.txt", ctx=_mock_ctx(fake_client))

    assert [r.path for r in result.results] == ["/visible.txt"]


async def test_find_by_type_filters_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})
    fake_client.webdav.find_by_type = AsyncMock(
        return_value=[
            {"path": "/Secret.txt", "name": "Secret.txt", "is_directory": False},
            {"path": "/visible.txt", "name": "visible.txt", "is_directory": False},
        ]
    )

    fn = webdav_tools["nc_webdav_find_by_type"].fn
    result = await fn(mime_type="text/plain", ctx=_mock_ctx(fake_client))

    assert [r.path for r in result.results] == ["/visible.txt"]


async def test_list_favorites_filters_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Secret.txt"})
    fake_client.webdav.list_favorites = AsyncMock(
        return_value=[
            {"path": "/Secret.txt", "name": "Secret.txt", "is_directory": False},
            {"path": "/visible.txt", "name": "visible.txt", "is_directory": False},
        ]
    )

    fn = webdav_tools["nc_webdav_list_favorites"].fn
    result = await fn(ctx=_mock_ctx(fake_client))

    assert [r.path for r in result.results] == ["/visible.txt"]


# ── Search-tool scope guards (review #764) ──────────────────────────────


async def test_search_files_raises_when_scope_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    """Mirror the ``list_directory`` early guard so the four search tools
    cannot silently return an empty result for an excluded ``scope``."""
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_search_files"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(ctx=_mock_ctx(fake_client), scope="/Private", name_pattern="%.txt")

    fake_client.webdav.search_files.assert_not_called()


async def test_find_by_name_raises_when_scope_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_find_by_name"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(pattern="%.txt", scope="/Private", ctx=_mock_ctx(fake_client))

    fake_client.webdav.find_by_name.assert_not_called()


async def test_find_by_type_raises_when_scope_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_find_by_type"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(mime_type="text/plain", scope="/Private", ctx=_mock_ctx(fake_client))

    fake_client.webdav.find_by_type.assert_not_called()


async def test_list_favorites_raises_when_scope_excluded(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    patch_get_client(fake_client)
    patch_excluded({"Private"})

    fn = webdav_tools["nc_webdav_list_favorites"].fn
    with pytest.raises(ToolError, match="excluded tag"):
        await fn(ctx=_mock_ctx(fake_client), scope="/Private")

    fake_client.webdav.list_favorites.assert_not_called()
