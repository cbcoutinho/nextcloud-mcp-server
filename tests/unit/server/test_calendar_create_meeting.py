"""Unit tests for the timezone ``nc_calendar_create_meeting`` forwards (GH #1502).

The tool builds naive ``{date}T{time}`` datetimes, so without a timezone every
meeting was stored as RFC 5545 floating time. It now takes ``timezone`` and,
when omitted, falls back to the user's Nextcloud timezone preference.
"""

from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.client.users import UsersClient
from nextcloud_mcp_server.server.calendar import configure_calendar_tools
from tests.client.conftest import create_mock_response

pytestmark = pytest.mark.unit


@pytest.fixture
def create_meeting_tool():
    mcp = MCPServer("test")
    configure_calendar_tools(mcp)
    return mcp._tool_manager.get_tool("nc_calendar_create_meeting")


@pytest.fixture
def client(mocker):
    client = mocker.MagicMock()
    client.calendar.create_event = mocker.AsyncMock(return_value={"uid": "u"})
    client.users.get_current_user_timezone = mocker.AsyncMock(
        return_value="Europe/Amsterdam"
    )
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        mocker.AsyncMock(return_value=client),
    )
    # Pin the deployment mode; see test_calendar_create_recurrence.py.
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    return client


async def _forwarded_timezone(tool, client, mocker, **kwargs) -> str:
    await tool.fn(
        title="Sync", date="2026-09-21", time="14:00", ctx=mocker.MagicMock(), **kwargs
    )
    return client.calendar.create_event.call_args.args[1]["timezone"]


async def test_explicit_timezone_wins_without_a_lookup(
    create_meeting_tool, client, mocker
):
    tz = await _forwarded_timezone(
        create_meeting_tool, client, mocker, timezone="America/New_York"
    )

    assert tz == "America/New_York"
    client.users.get_current_user_timezone.assert_not_awaited()


async def test_defaults_to_the_users_nextcloud_timezone(
    create_meeting_tool, client, mocker
):
    assert await _forwarded_timezone(create_meeting_tool, client, mocker) == (
        "Europe/Amsterdam"
    )


async def test_lookup_failure_falls_back_to_floating(
    create_meeting_tool, client, mocker
):
    client.users.get_current_user_timezone.side_effect = httpx.ConnectError("down")

    assert await _forwarded_timezone(create_meeting_tool, client, mocker) == ""


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"id": "alice", "timezone": "Europe/Berlin"}, "Europe/Berlin"),
        ({"id": "alice", "timezone": ""}, ""),
        ({"id": "alice"}, ""),
    ],
)
async def test_get_current_user_timezone_reads_cloud_user(mocker, data, expected):
    make_request = mocker.patch.object(
        UsersClient,
        "_make_request",
        return_value=create_mock_response(json_data={"ocs": {"data": data}}),
    )
    users = UsersClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")

    assert await users.get_current_user_timezone() == expected
    assert make_request.call_args.args == ("GET", "/ocs/v2.php/cloud/user")
