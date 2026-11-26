"""Unit tests for the Activity tools: EXCLUDED_TAGS filtering and error mapping."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from nextcloud_client.models.activity import ActivityPage

from nextcloud_mcp_server.server.activity import (
    activity_file_paths,
    configure_activity_tools,
)

pytestmark = pytest.mark.unit

MODULE = "nextcloud_mcp_server.server.activity"


def _activity(activity_id: int, **fields) -> dict:
    return {
        "activity_id": activity_id,
        "datetime": "2026-11-26T10:00:00+00:00",
        "app": "files",
        "type": "file_changed",
        "subject": "You changed a file",
        "object_type": "files",
        "objects": {},
        **fields,
    }


def _page(activities, **fields) -> ActivityPage:
    return ActivityPage(
        activities=activities,
        last_given=fields.get("last_given"),
        first_known=None,
        has_more=fields.get("has_more", False),
        etag=None,
        cursor_reset=fields.get("cursor_reset", False),
    )


@pytest.fixture
def client(mocker):
    fake = SimpleNamespace(activity=AsyncMock(), webdav=AsyncMock())
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    )
    mocker.patch(f"{MODULE}.get_client", AsyncMock(return_value=fake))
    mocker.patch(
        f"{MODULE}.get_excluded_file_paths", AsyncMock(return_value={"Private"})
    )
    return fake


@pytest.fixture
def tools() -> SimpleNamespace:
    mcp = MCPServer(name="test-activity-tools")
    configure_activity_tools(mcp)
    return SimpleNamespace(**{t.name: t.fn for t in mcp._tool_manager.list_tools()})


async def test_grouped_activity_touching_an_excluded_file_is_omitted(client, tools):
    """One hidden file in a grouped activity hides the whole activity."""
    client.activity.list_activities.return_value = _page(
        [
            _activity(1, object_name="/Public/a.md", objects={"1": "/Public/a.md"}),
            _activity(
                2,
                object_name="/Public/b.md",
                objects={"2": "/Public/b.md", "3": "/Private/secret.md"},
            ),
        ],
        last_given=2,
    )
    result = await tools.nc_activity_list(SimpleNamespace(), sort="asc", since=0)
    assert [a.activity_id for a in result.activities] == [1]
    assert result.excluded_count == 1
    # The cursor still advances past what was left out.
    assert result.next_since == 2


async def test_excluded_file_in_rich_subject_only(client, tools):
    hidden = _activity(
        1,
        object_type="calendar",
        subject_rich=["{file}", {"file": {"type": "file", "path": "Private/x"}}],
    )
    client.activity.list_activities.return_value = _page([hidden])
    result = await tools.nc_activity_list(SimpleNamespace())
    assert result.activities == []


async def test_objects_checked_for_non_file_activity(client, tools):
    """A comment or share activity naming a hidden file in `objects` is dropped."""
    client.activity.list_activities.return_value = _page(
        [_activity(1, object_type="comments", objects={"9": "/Private/x.md"})]
    )
    result = await tools.nc_activity_list(SimpleNamespace())
    assert result.excluded_count == 1


async def test_empty_page_keeps_the_cursor(client, tools):
    client.activity.list_activities.return_value = _page([])
    result = await tools.nc_activity_list(SimpleNamespace(), sort="asc", since=41)
    assert result.next_since == 41
    assert result.cursor_reset is False


async def test_object_id_selects_the_object_filter(client, tools):
    client.activity.list_activities.return_value = _page([])
    await tools.nc_activity_list(SimpleNamespace(), object_id=7)
    kwargs = client.activity.list_activities.await_args.kwargs
    assert (kwargs["object_type"], kwargs["object_id"]) == ("files", 7)


@pytest.mark.parametrize(
    ("status", "message"), [(404, "Unknown activity filter"), (403, "not valid")]
)
async def test_http_errors_are_tool_errors(client, tools, status, message):
    response = httpx2.Response(status, request=httpx2.Request("GET", "http://x"))
    client.activity.list_activities.side_effect = httpx2.HTTPStatusError(
        "boom", request=response.request, response=response
    )
    with pytest.raises(ToolError, match=message):
        await tools.nc_activity_list(SimpleNamespace(), filter="x", since=5)


async def test_403_without_a_cursor_does_not_blame_the_cursor(client, tools):
    response = httpx2.Response(403, request=httpx2.Request("GET", "http://x"))
    client.activity.list_activities.side_effect = httpx2.HTTPStatusError(
        "boom", request=response.request, response=response
    )
    with pytest.raises(ToolError, match="HTTP 403"):
        await tools.nc_activity_list(SimpleNamespace())


_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
)


@given(subject_rich=_json, object_name=_json)
def test_file_paths_never_raises_on_arbitrary_json(subject_rich, object_name):
    """subject_rich comes from the server: any JSON shape must be tolerated."""
    paths = activity_file_paths(
        {
            "object_type": "files",
            "objects": {},
            "object_name": object_name,
            "subject_rich": subject_rich,
        }
    )
    assert all(isinstance(p, str) for p in paths)
