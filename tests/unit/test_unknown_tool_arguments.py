"""Unknown tool arguments are refused, not silently dropped (card #1460).

``deck_update_card(..., new_stack_id=45)`` used to report success while the
card stayed put: the tool has no ``new_stack_id`` and the SDK's argument model
ignores extra keys. ``NextcloudMCPServer`` now refuses them at dispatch.
"""

import functools

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server import errors
from nextcloud_mcp_server.errors import NextcloudMCPServer
from nextcloud_mcp_server.server import AVAILABLE_APPS, configure_app_tools

pytestmark = pytest.mark.unit


@functools.cache
def _server() -> NextcloudMCPServer:
    mcp = NextcloudMCPServer("unknown-args")
    for app in AVAILABLE_APPS:
        configure_app_tools(mcp, app)
    return mcp


TOOL_NAMES = sorted(t.name for t in _server()._tool_manager.list_tools())


@given(name=st.sampled_from(TOOL_NAMES), key=st.text(min_size=1))
def test_any_undeclared_key_is_refused_for_every_tool(name, key):
    mcp = _server()
    properties = mcp._tool_manager.get_tool(name).parameters.get("properties", {})
    assume(key not in properties)

    with pytest.raises(ToolError, match="unknown argument"):
        mcp._reject_unknown_arguments(name, {key: 1})


@given(name=st.sampled_from(TOOL_NAMES))
def test_declared_keys_pass(name):
    mcp = _server()
    properties = mcp._tool_manager.get_tool(name).parameters.get("properties", {})

    mcp._reject_unknown_arguments(name, dict.fromkeys(properties))


async def test_refused_call_never_reaches_the_tool(mocker):
    super_call = mocker.patch.object(MCPServer, "call_tool")
    gate = mocker.patch.object(errors, "enforce_capability")

    with pytest.raises(ToolError) as exc:
        await _server().call_tool(
            "deck_update_card",
            {"board_id": 11, "stack_id": 43, "card_id": 1455, "new_stack_id": 45},
        )

    message = str(exc.value)
    assert message.startswith("deck_update_card: unknown argument(s) 'new_stack_id';")
    assert "valid: board_id, stack_id, card_id" in message
    super_call.assert_not_called()
    gate.assert_not_called()


async def test_known_arguments_reach_the_tool(mocker):
    super_call = mocker.patch.object(MCPServer, "call_tool", return_value="ok")
    mocker.patch.object(errors, "enforce_capability")
    mocker.patch.object(errors, "compact_tool_result", side_effect=lambda r: r)

    args = {"board_id": 11, "stack_id": 43, "card_id": 1455}
    assert await _server().call_tool("deck_update_card", args) == "ok"
    super_call.assert_awaited_once()


async def test_listed_schemas_forbid_additional_properties(mocker):
    mocker.patch.object(errors, "filter_by_capability", side_effect=lambda _, t: t)
    mcp = _server()

    tools = await mcp.list_tools()

    assert tools
    assert all(t.input_schema["additionalProperties"] is False for t in tools)
    # The advertised copy must not leak into the shared Tool.parameters.
    assert all(
        "additionalProperties" not in t.parameters
        for t in mcp._tool_manager.list_tools()
    )
