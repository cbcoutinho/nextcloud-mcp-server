"""What the calendar tools forward for transparency, and what manage_calendar returns.

GH #1604: events and calendars reported their free/busy transparency, but no
tool could set it. ``nc_calendar_manage_calendar(action="update")`` did not
change anything at all and answered with a raw ``not_implemented`` dict.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.models.calendar import ManageCalendarResponse
from nextcloud_mcp_server.server.calendar import configure_calendar_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def client(mocker):
    client = mocker.MagicMock()
    client.calendar.create_event = mocker.AsyncMock(return_value={"uid": "u"})
    client.calendar.update_event = mocker.AsyncMock(return_value={"uid": "u"})
    client.calendar.bulk_update_events = mocker.AsyncMock(return_value={})
    client.calendar.create_calendar = mocker.AsyncMock(
        side_effect=lambda **kwargs: {
            "name": kwargs["calendar_name"],
            "display_name": kwargs["display_name"],
            "description": kwargs["description"],
            "color": kwargs["color"],
            "transparent": kwargs["transparency"] == "TRANSPARENT",
            "status_code": 201,
        }
    )
    client.calendar.update_calendar = mocker.AsyncMock(
        return_value={"name": "info", "display_name": "Info", "transparent": True}
    )
    client.calendar.delete_calendar = mocker.AsyncMock(return_value={})
    client.calendar.list_calendars = mocker.AsyncMock(
        return_value=[{"name": "work", "display_name": "Work"}]
    )
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        mocker.AsyncMock(return_value=client),
    )
    # @require_scopes denies a context-bearing request without a verified token
    # only under login-flow; pin it off so the tools run in the unit harness.
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    return client


@pytest.fixture
def tools():
    mcp = MCPServer("test")
    configure_calendar_tools(mcp)
    return lambda name: mcp._tool_manager.get_tool(name).fn


class TestEventTools:
    async def test_create_defaults_to_opaque(self, tools, client, mocker):
        await tools("nc_calendar_create_event")(
            calendar_name="personal",
            title="Standup",
            start_datetime="2026-02-10T10:00:00Z",
            ctx=mocker.MagicMock(),
        )

        assert client.calendar.create_event.call_args.args[1]["transparency"] == (
            "OPAQUE"
        )

    async def test_create_forwards_transparent(self, tools, client, mocker):
        await tools("nc_calendar_create_event")(
            calendar_name="personal",
            title="Deadline",
            start_datetime="2026-02-10",
            all_day=True,
            transparency="TRANSPARENT",
            ctx=mocker.MagicMock(),
        )

        assert client.calendar.create_event.call_args.args[1]["transparency"] == (
            "TRANSPARENT"
        )

    async def test_update_leaves_transparency_alone_when_unset(
        self, tools, client, mocker
    ):
        await tools("nc_calendar_update_event")(
            calendar_name="personal", event_uid="u", title="New", ctx=mocker.MagicMock()
        )

        assert "transparency" not in client.calendar.update_event.call_args.args[2]

    async def test_update_forwards_transparency(self, tools, client, mocker):
        await tools("nc_calendar_update_event")(
            calendar_name="personal",
            event_uid="u",
            transparency="TRANSPARENT",
            ctx=mocker.MagicMock(),
        )

        assert client.calendar.update_event.call_args.args[2] == {
            "transparency": "TRANSPARENT"
        }

    async def test_bulk_update_forwards_new_transparency(self, tools, client, mocker):
        await tools("nc_calendar_bulk_operations")(
            operation="update",
            calendar_name="info",
            new_transparency="TRANSPARENT",
            ctx=mocker.MagicMock(),
        )

        update_data = client.calendar.bulk_update_events.call_args.args[1]
        assert update_data == {"transparency": "TRANSPARENT"}


class TestManageCalendar:
    async def test_list_returns_the_response_model(self, tools, client, mocker):
        result = await tools("nc_calendar_manage_calendar")(
            action="list", ctx=mocker.MagicMock()
        )

        assert isinstance(result, ManageCalendarResponse)
        assert [c.name for c in result.calendars or []] == ["work"]
        assert result.total_count == 1

    async def test_create_forwards_transparency_and_keeps_the_default_color(
        self, tools, client, mocker
    ):
        result = await tools("nc_calendar_manage_calendar")(
            action="create",
            calendar_name="info",
            transparency="TRANSPARENT",
            ctx=mocker.MagicMock(),
        )

        kwargs = client.calendar.create_calendar.call_args.kwargs
        assert kwargs["color"] == "#1976D2"
        assert kwargs["display_name"] == "info"
        assert kwargs["transparency"] == "TRANSPARENT"
        assert isinstance(result, ManageCalendarResponse)
        assert result.calendar is not None
        assert result.calendar.transparent is True

    async def test_update_sends_only_what_was_given(self, tools, client, mocker):
        """An omitted colour must not reset the stored one to the create default."""
        result = await tools("nc_calendar_manage_calendar")(
            action="update",
            calendar_name="info",
            transparency="TRANSPARENT",
            ctx=mocker.MagicMock(),
        )

        client.calendar.update_calendar.assert_awaited_once_with(
            "info",
            display_name=None,
            description=None,
            color=None,
            transparency="TRANSPARENT",
        )
        assert result.action == "update"
        assert result.calendar is not None
        assert result.calendar.transparent is True

    async def test_update_error_becomes_tool_error(self, tools, client, mocker):
        client.calendar.update_calendar.side_effect = RuntimeError(
            "Failed to update calendar 'info': HTTP 403"
        )
        manage = tools("nc_calendar_manage_calendar")
        ctx = mocker.MagicMock()

        with pytest.raises(ToolError, match="HTTP 403"):
            await manage(
                action="update", calendar_name="info", color="#000000", ctx=ctx
            )

    async def test_delete_returns_the_response_model(self, tools, client, mocker):
        result = await tools("nc_calendar_manage_calendar")(
            action="delete", calendar_name="info", ctx=mocker.MagicMock()
        )

        client.calendar.delete_calendar.assert_awaited_once_with("info")
        assert isinstance(result, ManageCalendarResponse)
        assert result.action == "delete"

    @pytest.mark.parametrize("action", ["create", "delete", "update"])
    async def test_calendar_name_is_required(self, tools, client, mocker, action):
        manage = tools("nc_calendar_manage_calendar")
        ctx = mocker.MagicMock()

        with pytest.raises(ToolError, match="calendar_name is required"):
            await manage(action=action, ctx=ctx)
