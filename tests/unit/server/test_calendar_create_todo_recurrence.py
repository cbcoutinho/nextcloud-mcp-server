"""Unit tests for what ``nc_calendar_create_todo`` forwards to the client.

``nc_calendar_create_event`` already accepts ``recurrence_rule`` /
``recurrence_end_date``. Until #1658 the todo tool had neither, so agents
could not create a recurring task even though the client read path expands
VTODO RRULEs. These tests pin the tool wiring; iCal emission is covered in
``tests/unit/client/test_calendar_create_todo_recurrence.py``.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.server.calendar import configure_calendar_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def create_todo_tool():
    mcp = MCPServer("test")
    configure_calendar_tools(mcp)
    return mcp._tool_manager.get_tool("nc_calendar_create_todo")


async def _captured_todo_data(tool, mocker, **kwargs):
    """Call the tool with a stubbed client and return the dict it forwarded."""
    client = mocker.MagicMock()
    client.calendar.create_todo = mocker.AsyncMock(return_value={"uid": "u"})
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        mocker.AsyncMock(return_value=client),
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )

    await tool.fn(
        calendar_name="personal",
        summary="Weekly backup check",
        ctx=mocker.MagicMock(),
        **kwargs,
    )

    return client.calendar.create_todo.call_args.args[1]


async def test_create_todo_forwards_recurrence_rule(create_todo_tool, mocker):
    todo_data = await _captured_todo_data(
        create_todo_tool,
        mocker,
        recurrence_rule="FREQ=WEEKLY;BYDAY=MO",
        dtstart="2026-02-10",
        due="2026-02-10",
    )

    assert todo_data["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO"
    assert todo_data["recurrence_end_date"] == ""


async def test_create_todo_forwards_recurrence_end_date(create_todo_tool, mocker):
    todo_data = await _captured_todo_data(
        create_todo_tool,
        mocker,
        recurrence_rule="FREQ=MONTHLY",
        recurrence_end_date="2026-12-31",
        dtstart="2026-02-10",
    )

    assert todo_data["recurrence_rule"] == "FREQ=MONTHLY"
    assert todo_data["recurrence_end_date"] == "2026-12-31"


async def test_create_todo_default_recurrence_fields_are_empty(
    create_todo_tool, mocker
):
    """Empty strings keep the client on the non-recurring path."""
    todo_data = await _captured_todo_data(create_todo_tool, mocker)

    assert todo_data["recurrence_rule"] == ""
    assert todo_data["recurrence_end_date"] == ""
