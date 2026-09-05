"""Unit tests for what ``nc_calendar_create_event`` forwards to the client.

The tool builds ``event_data`` as a flat dict of every parameter, which meant a
``bool`` argument with a ``False`` default was indistinguishable from one the
caller actually set. ``recurring`` was such a parameter: the client treats a
present-but-False flag as an explicit opt-out, so every call pinned it off and
``recurrence_rule`` was silently dropped no matter what the caller passed.

Testing at the client layer misses this entirely — ``_create_ical_event``
behaves correctly when the key is absent, and it is only absent if the *tool*
leaves it out. CI's single-user integration lane is what surfaced it.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.server.calendar import configure_calendar_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def create_event_tool():
    mcp = MCPServer("test")
    configure_calendar_tools(mcp)
    return mcp._tool_manager.get_tool("nc_calendar_create_event")


async def _captured_event_data(tool, mocker, **kwargs):
    """Call the tool with a stubbed client and return the dict it forwarded."""
    client = mocker.MagicMock()
    client.calendar.create_event = mocker.AsyncMock(return_value={"uid": "u"})
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        mocker.AsyncMock(return_value=client),
    )
    # Pin the deployment mode. @require_scopes denies a request that carries a
    # context but no verified token *only* under login-flow, so leaving this to
    # the ambient environment makes the test pass locally and fail in CI.
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )

    await tool.fn(
        calendar_name="personal",
        title="Standup",
        start_datetime="2026-02-10T10:00:00Z",
        ctx=mocker.MagicMock(),
        **kwargs,
    )

    return client.calendar.create_event.call_args.args[1]


async def test_recurring_is_omitted_when_the_caller_does_not_set_it(
    create_event_tool, mocker
):
    """An unset flag must not reach the client as False.

    The client reads ``recurring=False`` as "suppress the series", so forwarding
    the default turned every ``recurrence_rule`` into a no-op.
    """
    event_data = await _captured_event_data(
        create_event_tool, mocker, recurrence_rule="FREQ=WEEKLY;BYDAY=TU"
    )

    assert "recurring" not in event_data
    assert event_data["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=TU"


@pytest.mark.parametrize("value", [True, False])
async def test_recurring_is_forwarded_when_explicitly_set(
    create_event_tool, mocker, value
):
    """Both explicit values still reach the client, including False."""
    event_data = await _captured_event_data(
        create_event_tool, mocker, recurrence_rule="FREQ=DAILY", recurring=value
    )

    assert event_data["recurring"] is value


async def test_reminders_are_omitted_when_unset(create_event_tool, mocker):
    """Same distinction for reminders: absent preserves, ``[]`` clears."""
    event_data = await _captured_event_data(create_event_tool, mocker)

    assert "reminders" not in event_data
