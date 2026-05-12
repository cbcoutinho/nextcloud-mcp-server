"""Integration tests for the Nextcloud Talk (spreed) MCP tools."""

import json
import logging

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


EXPECTED_TALK_TOOLS = {
    "talk_list_conversations",
    "talk_get_conversation",
    "talk_get_messages",
    "talk_list_participants",
    "talk_send_message",
    "talk_mark_as_read",
}


async def test_talk_mcp_connectivity(nc_mcp_client: ClientSession):
    """All six Talk tools should be registered with the MCP server."""
    tools = await nc_mcp_client.list_tools()
    tool_names = {tool.name for tool in tools.tools}

    missing = EXPECTED_TALK_TOOLS - tool_names
    assert not missing, f"Missing Talk tools: {missing}"


async def test_talk_send_and_read_workflow(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    temporary_conversation: dict,
):
    """End-to-end: post a message via MCP, read it back, mark read."""
    token = temporary_conversation["token"]

    # 1. Send a message via MCP
    send_result = await nc_mcp_client.call_tool(
        "talk_send_message",
        {"token": token, "message": "Hello from MCP integration test"},
    )
    assert send_result.isError is False, (
        f"talk_send_message failed: {send_result.content}"
    )
    send_payload = json.loads(send_result.content[0].text)
    assert send_payload["success"] is True
    posted = send_payload["message"]
    assert posted["message"] == "Hello from MCP integration test"
    assert posted["token"] == token
    posted_id = posted["id"]
    logger.info("Posted message id=%s into token=%s", posted_id, token)

    # 2. Cross-check via direct client
    direct_messages, _ = await nc_client.talk.get_messages(token, limit=10)
    direct_ids = [m.id for m in direct_messages]
    assert posted_id in direct_ids, "Posted message not visible via direct client"

    # 3. Read messages via MCP
    get_result = await nc_mcp_client.call_tool(
        "talk_get_messages", {"token": token, "limit": 10}
    )
    assert get_result.isError is False, (
        f"talk_get_messages failed: {get_result.content}"
    )
    get_payload = json.loads(get_result.content[0].text)
    assert get_payload["conversation_token"] == token
    listed_ids = [m["id"] for m in get_payload["results"]]
    assert posted_id in listed_ids, "Posted message not in MCP get_messages results"

    # 4. Mark conversation as read up to that message
    mark_result = await nc_mcp_client.call_tool(
        "talk_mark_as_read",
        {"token": token, "last_read_message": posted_id},
    )
    assert mark_result.isError is False, (
        f"talk_mark_as_read failed: {mark_result.content}"
    )
    mark_payload = json.loads(mark_result.content[0].text)
    assert mark_payload["success"] is True
    assert mark_payload["conversation_token"] == token
    assert mark_payload["last_read_message"] == posted_id


async def test_talk_list_conversations_includes_temp_room(
    nc_mcp_client: ClientSession, temporary_conversation: dict
):
    """Newly created conversation should appear in talk_list_conversations."""
    token = temporary_conversation["token"]

    list_result = await nc_mcp_client.call_tool("talk_list_conversations", {})
    assert list_result.isError is False, (
        f"talk_list_conversations failed: {list_result.content}"
    )
    payload = json.loads(list_result.content[0].text)
    tokens = [r["token"] for r in payload["results"]]
    assert token in tokens, "Temporary conversation not found in list"


async def test_talk_get_conversation(
    nc_mcp_client: ClientSession, temporary_conversation: dict
):
    """talk_get_conversation returns the same room we created."""
    token = temporary_conversation["token"]
    name = temporary_conversation["name"]

    result = await nc_mcp_client.call_tool("talk_get_conversation", {"token": token})
    assert result.isError is False, f"talk_get_conversation failed: {result.content}"
    payload = json.loads(result.content[0].text)
    conversation = payload["conversation"]
    assert conversation["token"] == token
    assert conversation["name"] == name


async def test_talk_list_participants(
    nc_mcp_client: ClientSession, temporary_conversation: dict
):
    """talk_list_participants returns the room creator as a participant."""
    token = temporary_conversation["token"]

    result = await nc_mcp_client.call_tool("talk_list_participants", {"token": token})
    assert result.isError is False, f"talk_list_participants failed: {result.content}"
    payload = json.loads(result.content[0].text)
    assert payload["conversation_token"] == token
    actor_ids = [p["actorId"] for p in payload["results"]]
    # The user that created the room is always a participant.
    assert len(actor_ids) >= 1


@pytest.mark.parametrize("blank_text", ["", "   ", "\t\n", " \t \n "])
async def test_talk_send_message_validation_blank_text(
    nc_mcp_client: ClientSession,
    temporary_conversation: dict,
    blank_text: str,
):
    """Empty and whitespace-only message text are rejected client-side."""
    token = temporary_conversation["token"]

    result = await nc_mcp_client.call_tool(
        "talk_send_message", {"token": token, "message": blank_text}
    )
    assert result.isError is True, (
        f"Expected validation error for blank message {blank_text!r}"
    )


async def test_talk_send_message_validation_too_long(
    nc_mcp_client: ClientSession, temporary_conversation: dict
):
    """A message exceeding the 32000-char ceiling is rejected client-side."""
    token = temporary_conversation["token"]

    result = await nc_mcp_client.call_tool(
        "talk_send_message",
        {"token": token, "message": "x" * 32001},
    )
    assert result.isError is True, (
        "Expected validation error for message longer than 32000 characters"
    )
