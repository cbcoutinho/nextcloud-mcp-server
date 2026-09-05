"""Unit tests for how ``nc_share_create`` surfaces an invalid pairing.

The rule itself lives in the client layer (covered in
``tests/client/test_sharing_client.py``); what is pinned here is the tool's
translation of that rejection into a ``ToolError``. The caller has to be able to
*read* why the share was refused -- a public link that quietly published a file
is the failure this whole path exists to prevent, so an opaque internal error
would defeat the point.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.client.sharing import validate_share_with
from nextcloud_mcp_server.server.sharing import configure_sharing_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def share_create_tool():
    mcp = MCPServer("test")
    configure_sharing_tools(mcp)
    return mcp._tool_manager.get_tool("nc_share_create")


@pytest.fixture
def stub_client(mocker):
    """A client whose sharing layer keeps the real validation."""
    client = mocker.MagicMock()

    async def create_share(**kwargs):
        # Apply the real rule rather than a mock that would accept anything, so
        # the assertions below stay tied to the actual error messages. The valid
        # path is not exercised here -- it belongs to the client-layer tests.
        validate_share_with(kwargs.get("share_type", 0), kwargs.get("share_with"))
        return {"id": 1}

    client.sharing.create_share = create_share
    mocker.patch(
        "nextcloud_mcp_server.server.sharing.get_client",
        mocker.AsyncMock(return_value=client),
    )
    # Pin the deployment mode: @require_scopes denies a context without a
    # verified token only under login-flow, which would otherwise make this
    # pass locally and fail in CI.
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    return client


async def test_public_link_with_recipient_raises_a_readable_tool_error(
    share_create_tool, stub_client, mocker
):
    with pytest.raises(ToolError) as exc_info:
        await share_create_tool.fn(
            path="/Secrets/salaries.xlsx",
            share_with="alice",
            share_type=3,
            ctx=mocker.MagicMock(),
        )

    # The message must name the consequence, not just say "invalid".
    message = str(exc_info.value)
    assert "must not carry shareWith" in message
    assert "anyone holding the URL" in message
    # ...and point at a tool this caller can actually invoke. The client-layer
    # message deliberately names no call, because its own method names do not
    # exist on the MCP side.
    assert "nc_share_create_public_link" in message
    assert "create_public_link()" not in message


async def test_missing_recipient_raises_a_tool_error(
    share_create_tool, stub_client, mocker
):
    with pytest.raises(ToolError, match="requires a non-empty shareWith"):
        await share_create_tool.fn(
            path="/Documents/report.md",
            share_type=0,
            ctx=mocker.MagicMock(),
        )


async def test_missing_recipient_does_not_suggest_the_public_link_tool(
    share_create_tool, stub_client, mocker
):
    """A wrong redirect is the failure this message path exists to avoid.

    The public-link suggestion belongs only to the public-link-with-recipient
    rejection. Appending it to "you forgot the recipient" would send a caller
    who wants a *user* share off to create an anonymous link instead.
    """
    with pytest.raises(ToolError) as exc_info:
        await share_create_tool.fn(
            path="/Documents/report.md",
            share_type=0,
            ctx=mocker.MagicMock(),
        )

    assert "nc_share_create_public_link" not in str(exc_info.value)
