"""Integration tests for the Nextcloud Talk (spreed) MCP tools."""

import json
import logging
import uuid

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
    "talk_create_conversation",
    "talk_add_participant",
    "talk_list_reactions",
    "talk_react",
    "talk_remove_reaction",
}


async def test_talk_mcp_connectivity(nc_mcp_client: ClientSession):
    """Every Talk tool should be registered with the MCP server."""
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
    assert send_result.is_error is False, (
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
    assert get_result.is_error is False, (
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
    assert mark_result.is_error is False, (
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
    assert list_result.is_error is False, (
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
    assert result.is_error is False, f"talk_get_conversation failed: {result.content}"
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
    assert result.is_error is False, f"talk_list_participants failed: {result.content}"
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
    assert result.is_error is True, (
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
    assert result.is_error is True, (
        "Expected validation error for message longer than 32000 characters"
    )


async def test_talk_create_conversation_and_add_participant(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Create a room through MCP, then add a participant to it.

    Both halves run together because the second needs a room the first
    produced -- and creating one via the tool is the only way to know the tool
    returns a usable token rather than merely reporting success.
    """
    room_name = f"MCP Created Room {uuid.uuid4().hex[:8]}"
    token = None
    try:
        create_result = await nc_mcp_client.call_tool(
            "talk_create_conversation", {"room_name": room_name}
        )
        assert create_result.is_error is False, create_result.content[0].text

        payload = json.loads(create_result.content[0].text)
        assert payload["success"] is True
        conversation = payload["conversation"]
        token = conversation["token"]
        assert conversation["name"] == room_name

        # The room is real: fetching it back through a different tool works.
        fetched = await nc_mcp_client.call_tool(
            "talk_get_conversation", {"token": token}
        )
        assert fetched.is_error is False
        assert json.loads(fetched.content[0].text)["conversation"]["token"] == token

        add_result = await nc_mcp_client.call_tool(
            "talk_add_participant", {"token": token, "participant": "admin"}
        )
        assert add_result.is_error is False, add_result.content[0].text
        add_payload = json.loads(add_result.content[0].text)
        assert add_payload["success"] is True
        assert add_payload["participant"] == "admin"
        assert add_payload["source"] == "users"
    finally:
        if token:
            await nc_client.talk.delete_conversation(token)


async def test_talk_reaction_round_trip(
    nc_mcp_client: ClientSession, temporary_conversation: dict
):
    """React to a message, see it listed, remove it, see it gone.

    The whole cycle in one test because each assertion is only meaningful
    relative to the state the previous step left behind.
    """
    token = temporary_conversation["token"]
    emoji = "\N{PARTY POPPER}"

    send = await nc_mcp_client.call_tool(
        "talk_send_message", {"token": token, "message": "react to me"}
    )
    message_id = json.loads(send.content[0].text)["message"]["id"]

    # Nothing yet.
    listed = await nc_mcp_client.call_tool(
        "talk_list_reactions", {"token": token, "message_id": message_id}
    )
    assert listed.is_error is False, listed.content[0].text
    assert json.loads(listed.content[0].text)["reactions"] == {}

    # React, and get the updated set back without a follow-up read.
    reacted = await nc_mcp_client.call_tool(
        "talk_react",
        {"token": token, "message_id": message_id, "reaction": emoji},
    )
    assert reacted.is_error is False, reacted.content[0].text
    react_payload = json.loads(reacted.content[0].text)
    assert emoji in react_payload["reactions"]
    assert react_payload["distinct_emoji"] == 1
    assert [a["actorId"] for a in react_payload["reactions"][emoji]] == ["admin"]

    # Reacting again with the same emoji leaves one reaction, not two -- this
    # is the claim talk_react's idempotent_hint makes.
    again = await nc_mcp_client.call_tool(
        "talk_react",
        {"token": token, "message_id": message_id, "reaction": emoji},
    )
    assert again.is_error is False
    assert len(json.loads(again.content[0].text)["reactions"][emoji]) == 1

    removed = await nc_mcp_client.call_tool(
        "talk_remove_reaction",
        {"token": token, "message_id": message_id, "reaction": emoji},
    )
    assert removed.is_error is False, removed.content[0].text
    assert json.loads(removed.content[0].text)["reactions"] == {}


async def test_talk_add_participant_rejects_blank_participant(
    nc_mcp_client: ClientSession, temporary_conversation: dict
):
    """A blank participant is refused before the request is built."""
    result = await nc_mcp_client.call_tool(
        "talk_add_participant",
        {"token": temporary_conversation["token"], "participant": "   "},
    )

    assert result.is_error is True
    assert "whitespace" in result.content[0].text.lower()


async def test_talk_create_conversation_rejects_blank_name(
    nc_mcp_client: ClientSession,
):
    """An unnamed group room is refused locally rather than at the server."""
    result = await nc_mcp_client.call_tool(
        "talk_create_conversation", {"room_name": "  "}
    )

    assert result.is_error is True
    assert "whitespace" in result.content[0].text.lower()


async def test_talk_add_participant_is_idempotent(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Adding the same participant twice is a no-op, not an error or a duplicate.

    This is the claim `talk_add_participant`'s `idempotent_hint=True` makes, and
    it was the one idempotency claim in this PR resting on a manual probe rather
    than a test -- the reaction tools got a re-apply assertion and this did not.
    """
    room_name = f"MCP Idempotency Room {uuid.uuid4().hex[:8]}"
    token = None
    try:
        created = await nc_mcp_client.call_tool(
            "talk_create_conversation", {"room_name": room_name}
        )
        token = json.loads(created.content[0].text)["conversation"]["token"]

        first = await nc_mcp_client.call_tool(
            "talk_add_participant", {"token": token, "participant": "admin"}
        )
        assert first.is_error is False, first.content[0].text

        before = await nc_mcp_client.call_tool(
            "talk_list_participants", {"token": token}
        )
        count_before = json.loads(before.content[0].text)["count"]

        # Same call again: succeeds, and leaves the roster untouched.
        second = await nc_mcp_client.call_tool(
            "talk_add_participant", {"token": token, "participant": "admin"}
        )
        assert second.is_error is False, second.content[0].text

        after = await nc_mcp_client.call_tool(
            "talk_list_participants", {"token": token}
        )
        assert json.loads(after.content[0].text)["count"] == count_before
    finally:
        if token:
            await nc_client.talk.delete_conversation(token)


async def test_talk_create_one_to_one_conversation_without_a_name(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, test_user
):
    """A one-to-one room is created from the invitee alone, with no room_name.

    The tool used to require `room_name` for every room type, forcing callers
    to invent a name spreed ignores for one-to-one rooms. This is the path that
    was documented as supported and never exercised.
    """
    await nc_client.users.create_user(**test_user)
    other = test_user["userid"]

    result = await nc_mcp_client.call_tool(
        "talk_create_conversation", {"room_type": 1, "invite": other}
    )
    assert result.is_error is False, result.content[0].text

    conversation = json.loads(result.content[0].text)["conversation"]
    # The wire key is "type", not the model's "room_type": that field carries
    # alias="type" and responses serialise by alias. Asserting the room kind --
    # rather than only that a token came back -- is what separates a real
    # one-to-one room from a group room that happens to contain the invitee.
    assert conversation["type"] == 1

    # The room really pairs the two. Read from `name` rather than the
    # participant list: on the create path spreed adds ONLY the actor --
    # RoomService::createOneToOneConversation calls addUsers() with the actor
    # alone, and the invitee is filled in later by ensureOneToOneRoomIsFilled,
    # which runs from the chat / call / room-GET controllers. So a freshly
    # created one-to-one room legitimately lists one participant.
    #
    # The pairing is in the room's stored name, which spreed sets to
    # json_encode(sort([actor, target])) and RoomFormatter then renders, for a
    # type-1 room, as whichever of the two is NOT the reader -- i.e. the
    # invitee. That is written synchronously at creation, so it is the field
    # that actually distinguishes a real pairing from a token.
    assert conversation["name"] == other

    # No room cleanup here on purpose: spreed answers DELETE on a one-to-one
    # room with 400 (reproduced on nc32/33/34). Those rooms are left, not
    # deleted -- which is why the conversation model carries a "former
    # one-to-one" type. The `test_user` fixture removes the other account,
    # which is what actually undoes the pairing.


async def test_talk_create_one_to_one_requires_an_invite(
    nc_mcp_client: ClientSession,
):
    """Without an invitee there is no second participant, so there is no room."""
    result = await nc_mcp_client.call_tool("talk_create_conversation", {"room_type": 1})

    assert result.is_error is True
    assert "invite is required" in result.content[0].text


async def test_talk_create_group_room_still_requires_a_name(
    nc_mcp_client: ClientSession,
):
    """Relaxing room_name for one-to-one must not relax it for group rooms."""
    result = await nc_mcp_client.call_tool(
        "talk_create_conversation", {"room_type": 2, "invite": "admin"}
    )

    assert result.is_error is True
    assert "room_name is required" in result.content[0].text


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"message_id": 0, "reaction": "\N{PARTY POPPER}"}, "message id"),
        ({"message_id": 1, "reaction": "   "}, "reaction must not be empty"),
        ({"message_id": 1, "reaction": "\N{PARTY POPPER}"}, None),
    ],
    ids=["bad-message-id", "blank-reaction", "control-valid-args"],
)
async def test_client_validation_surfaces_through_mcp(
    nc_mcp_client: ClientSession,
    temporary_conversation: dict,
    arguments: dict,
    expected: str | None,
):
    """Client-layer ValueErrors must reach the caller as a readable tool error.

    The blank-name and blank-participant paths raise `ToolError` at the tool
    layer and were already covered. These three validators raise plain
    `ValueError` in the client instead, and nothing asserted what that looks
    like after MCPServer has translated it -- a readable `isError` result or an
    opaque internal failure. The control case is included so the assertion is
    about the *rejections* rather than about every call failing.
    """
    token = temporary_conversation["token"]

    if expected is None:
        # Give the control case a message that really exists to react to.
        send = await nc_mcp_client.call_tool(
            "talk_send_message", {"token": token, "message": "seam control"}
        )
        arguments = {
            **arguments,
            "message_id": json.loads(send.content[0].text)["message"]["id"],
        }

    result = await nc_mcp_client.call_tool("talk_react", {"token": token, **arguments})

    if expected is None:
        assert result.is_error is False, result.content[0].text
        return

    assert result.is_error is True
    assert expected in result.content[0].text.lower()
