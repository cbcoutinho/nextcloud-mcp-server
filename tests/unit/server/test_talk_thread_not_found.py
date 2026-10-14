"""Only spreed's thread 404 becomes "thread not found" in talk_get_messages."""

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.client.talk import TalkClient
from nextcloud_mcp_server.server.talk import _get_messages

pytestmark = pytest.mark.unit


def _http_404(body: dict | None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://nc.example/ocs/v2.php/apps/spreed")
    response = httpx.Response(404, json=body, request=request)
    return httpx.HTTPStatusError("404 Not Found", request=request, response=response)


async def _call(mocker, error: httpx.HTTPStatusError):
    talk = mocker.AsyncMock(spec=TalkClient)
    talk.get_messages.side_effect = error
    return await _get_messages(
        talk,
        "abc",
        limit=50,
        last_known_message_id=None,
        include_last_known=False,
        thread_id=42,
    )


async def test_thread_404_becomes_tool_error(mocker):
    error = _http_404({"ocs": {"meta": {}, "data": {"error": "thread"}}})
    with pytest.raises(ToolError, match="Thread 42 not found"):
        await _call(mocker, error)


@pytest.mark.parametrize(
    "body",
    [
        {"ocs": {"meta": {}, "data": []}},  # unknown room / not a participant
        None,
    ],
    ids=["room-404", "no-body"],
)
async def test_other_404s_propagate(mocker, body):
    error = _http_404(body)
    with pytest.raises(httpx.HTTPStatusError):
        await _call(mocker, error)
