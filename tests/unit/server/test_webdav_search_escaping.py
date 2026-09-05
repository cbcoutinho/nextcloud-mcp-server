"""The SEARCH predicates ``nc_webdav_search_files`` builds must be escaped.

The client's ``find_by_*`` helpers escaped their literals; this tool did not,
so a ``name_pattern`` or ``mime_type`` containing ``&``/``<``/``>`` produced a
malformed SEARCH body that Sabre/DAV rejects with 400. The search then failed
outright rather than matching nothing, which is the confusing part -- the same
bug class as the folder-name-with-``&`` indexing gap and the empty-``<d:where>``
500.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.server.webdav import configure_webdav_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def search_tool():
    mcp = MCPServer("test")
    configure_webdav_tools(mcp)
    return mcp._tool_manager.get_tool("nc_webdav_search_files")


async def _captured_where(tool, mocker, **kwargs) -> str:
    """Call the tool with a stubbed client and return the SEARCH where-clause."""
    client = mocker.MagicMock()
    client.webdav.search_files = mocker.AsyncMock(return_value=[])
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client",
        mocker.AsyncMock(return_value=client),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_excluded_file_paths",
        mocker.AsyncMock(return_value=set()),
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )

    await tool.fn(ctx=mocker.MagicMock(), **kwargs)

    return client.webdav.search_files.call_args.kwargs["where_conditions"]


async def test_name_pattern_with_ampersand_yields_well_formed_xml(search_tool, mocker):
    where = await _captured_where(
        search_tool, mocker, scope="Docs", name_pattern="Costs & Revenue%"
    )

    # Raised ParseError on the bare '&' before the fix.
    ET.fromstring(f"<root xmlns:d='DAV:'>{where}</root>")
    assert "&amp;" in where
    assert "Costs & Revenue" not in where


async def test_mime_type_with_angle_brackets_yields_well_formed_xml(
    search_tool, mocker
):
    where = await _captured_where(
        search_tool, mocker, scope="Docs", mime_type="text/<odd>"
    )

    ET.fromstring(f"<root xmlns:d='DAV:'>{where}</root>")
    assert "&lt;odd&gt;" in where


async def test_combined_filters_stay_well_formed(search_tool, mocker):
    """Both predicates are escaped when AND-ed together."""
    where = await _captured_where(
        search_tool,
        mocker,
        scope="Docs",
        name_pattern="A & B%",
        mime_type="text/<x>",
    )

    root = ET.fromstring(f"<root xmlns:d='DAV:'>{where}</root>")
    assert root.find(".//{DAV:}and") is not None
    assert len(root.findall(".//{DAV:}like")) == 2
