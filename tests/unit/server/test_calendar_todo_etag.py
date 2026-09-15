"""Unit tests for ETag concurrency on the todo tools.

The CalDAV client layer has had real ``If-Match`` protection since #1335, but
only ``nc_calendar_update_event`` exposed it. The todo tools called
``update_todo`` with no ETag, so the write fell back to the one observed
*during that call* -- which closes the millisecond inside the call and leaves
the caller's actual read-modify-write cycle unguarded.

Two things are pinned here: that the caller's ETag reaches the client
unchanged, and that a stale one comes back as something the caller can act on.
A bare ``DavPreconditionFailed`` reaches an MCP client as an httpx status error
naming a CalDAV URL -- correct, and no help in deciding what to do next.
"""

from __future__ import annotations

import pytest
from httpx import Request, Response
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.client.dav_errors import DavPreconditionFailed
from nextcloud_mcp_server.server.calendar import configure_calendar_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def calendar_tools():
    mcp = MCPServer("test")
    configure_calendar_tools(mcp)
    return mcp._tool_manager


@pytest.fixture
def stub_client(mocker):
    """A client recording what the tool layer forwards to ``update_todo``."""
    client = mocker.MagicMock()
    client.calendar.update_todo = mocker.AsyncMock(
        return_value={"uid": "todo-1", "href": "/dav/todo-1.ics", "etag": '"new"'}
    )
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
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


def _precondition_failed(dav_message: str | None = None) -> DavPreconditionFailed:
    request = Request("PUT", "https://nc.example.com/dav/todo-1.ics")
    return DavPreconditionFailed(
        "412 Precondition Failed",
        request=request,
        response=Response(412, request=request),
        dav_exception="Sabre\\DAV\\Exception\\PreconditionFailed",
        dav_message=dav_message,
    )


@pytest.mark.parametrize(
    ("tool_name", "extra_kwargs"),
    [
        ("nc_calendar_update_todo", {"summary": "new title"}),
        ("nc_calendar_complete_todo", {}),
    ],
)
async def test_caller_etag_reaches_the_client(
    calendar_tools, stub_client, mocker, tool_name, extra_kwargs
):
    """Both write tools must forward the caller's ETag, not swallow it.

    Parametrized rather than written once for update: ``complete_todo`` builds
    its own payload and is the tool most likely to be given the ETag argument
    and then quietly drop it.
    """
    await calendar_tools.get_tool(tool_name).fn(
        calendar_name="Personal",
        todo_uid="todo-1",
        ctx=mocker.MagicMock(),
        etag='"caller-read-this"',
        **extra_kwargs,
    )

    # Positional, matching the client signature (…, todo_data, etag).
    assert stub_client.calendar.update_todo.call_args.args[3] == '"caller-read-this"'


@pytest.mark.parametrize(
    "tool_name",
    ["nc_calendar_update_todo", "nc_calendar_complete_todo"],
)
async def test_omitted_etag_forwards_empty_not_none(
    calendar_tools, stub_client, mocker, tool_name
):
    """The no-ETag case must stay the client's documented ``""`` sentinel.

    ``update_todo`` branches on ``if not etag`` to fall back to its own read.
    ``None`` would take the same branch today, so this pins the contract rather
    than a behaviour -- it is the kind of drift that only surfaces once someone
    tightens that check.
    """
    await calendar_tools.get_tool(tool_name).fn(
        calendar_name="Personal",
        todo_uid="todo-1",
        ctx=mocker.MagicMock(),
    )

    assert stub_client.calendar.update_todo.call_args.args[3] == ""


@pytest.mark.parametrize(
    "tool_name",
    ["nc_calendar_update_todo", "nc_calendar_complete_todo"],
)
async def test_stale_etag_becomes_a_readable_tool_error(
    calendar_tools, stub_client, mocker, tool_name
):
    """A 412 must name the recovery, not just the failure."""
    stub_client.calendar.update_todo.side_effect = _precondition_failed(
        "The ETag supplied in the If-Match header did not match"
    )

    with pytest.raises(ToolError) as exc_info:
        await calendar_tools.get_tool(tool_name).fn(
            calendar_name="Personal",
            todo_uid="todo-1",
            ctx=mocker.MagicMock(),
            etag='"stale"',
        )

    message = str(exc_info.value)
    # The server's own wording survives...
    assert "If-Match header did not match" in message
    # ...and the caller is told how to recover, by a tool that exists.
    assert "nc_calendar_list_todos" in message
    assert "todo-1" in message


@pytest.mark.parametrize(
    ("tool_name", "extra_kwargs"),
    [
        ("nc_calendar_update_todo", {"summary": "new title"}),
        ("nc_calendar_complete_todo", {}),
    ],
)
async def test_stale_etag_without_a_server_message_still_explains_itself(
    calendar_tools, stub_client, mocker, tool_name, extra_kwargs
):
    """Sabre does not always send ``s:message``; the advice must not vanish.

    Parametrized to match its "with a message" sibling. The message-building
    helper is shared, but each tool wires it up at its own call site -- so an
    asymmetric pair here is how a per-tool gap survives unnoticed.
    """
    stub_client.calendar.update_todo.side_effect = _precondition_failed(None)

    with pytest.raises(ToolError) as exc_info:
        await calendar_tools.get_tool(tool_name).fn(
            calendar_name="Personal",
            todo_uid="todo-1",
            ctx=mocker.MagicMock(),
            etag='"stale"',
            **extra_kwargs,
        )

    message = str(exc_info.value)
    assert "modified after that ETag was read" in message
    assert "nc_calendar_list_todos" in message


async def test_update_returns_the_new_etag_for_chaining(
    calendar_tools, stub_client, mocker
):
    """The post-write ETag has to come back, or the next update is unguarded."""
    result = await calendar_tools.get_tool("nc_calendar_update_todo").fn(
        calendar_name="Personal",
        todo_uid="todo-1",
        ctx=mocker.MagicMock(),
        summary="new title",
    )

    # Called through .fn, so this is the model instance, not serialised JSON.
    assert result.etag == '"new"'
    assert result.uid == "todo-1"
    assert result.calendar_name == "Personal"
    assert result.href == "/dav/todo-1.ics"


async def test_complete_returns_the_new_etag_alongside_what_it_wrote(
    calendar_tools, stub_client, mocker
):
    result = await calendar_tools.get_tool("nc_calendar_complete_todo").fn(
        calendar_name="Personal",
        todo_uid="todo-1",
        ctx=mocker.MagicMock(),
    )

    assert result.etag == '"new"'
    # The three completion properties are still the point of this tool.
    assert result.status == "COMPLETED"
    assert result.percent_complete == 100
    assert result.completed
