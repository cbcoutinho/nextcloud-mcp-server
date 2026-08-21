"""Unit tests for the Nextcloud Talk (spreed) HTTP client."""

import logging

import httpx
import pytest

from nextcloud_mcp_server.client.talk import TalkClient, _validate_token
from nextcloud_mcp_server.models.talk import (
    TalkConversation,
    TalkMessage,
    TalkParticipant,
)
from tests.client.conftest import (
    create_mock_error_response,
    create_mock_response,
    create_mock_talk_message_response,
    create_mock_talk_room_response,
)

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.unit


# Token validation


@pytest.mark.parametrize(
    "bad_token",
    [
        "",
        "../foo",
        "a/b",
        "a b",
        "a.b",
        "a-b",
        "token!",
    ],
)
def test_validate_token_rejects_invalid(bad_token):
    """_validate_token rejects anything outside the alphanumeric whitelist."""
    with pytest.raises(ValueError, match="Invalid Talk conversation token"):
        _validate_token(bad_token)


@pytest.mark.parametrize("good_token", ["a1b2c3d4", "ABC123", "abcdef", "1"])
def test_validate_token_accepts_valid(good_token):
    """Real spreed tokens — short alphanumeric strings — pass through."""
    _validate_token(good_token)


async def test_talk_get_conversation_rejects_path_traversal(mocker):
    """Pathological tokens never reach the HTTP layer."""
    mock_make_request = mocker.patch.object(TalkClient, "_make_request")

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(ValueError, match="Invalid Talk conversation token"):
        await client.get_conversation("../etc/passwd")

    mock_make_request.assert_not_called()


# Conversation tests


async def test_talk_list_conversations(mocker):
    """list_conversations parses the OCS-wrapped room list."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={
            "ocs": {
                "meta": {"status": "ok"},
                "data": [
                    {
                        "id": 1,
                        "token": "abc",
                        "type": 2,
                        "name": "Group 1",
                        "displayName": "Group 1",
                        "lastMessage": [],
                    },
                    {
                        "id": 2,
                        "token": "def",
                        "type": 3,
                        "name": "Public Room",
                        "displayName": "Public Room",
                        "lastMessage": [],
                    },
                ],
            }
        },
    )

    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    rooms = await client.list_conversations()

    assert len(rooms) == 2
    assert all(isinstance(r, TalkConversation) for r in rooms)
    assert rooms[0].token == "abc"
    assert rooms[1].room_type == 3

    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args
    assert call_args[0][0] == "GET"
    assert call_args[0][1] == "/ocs/v2.php/apps/spreed/api/v4/room"
    # noStatusUpdate defaults to True
    assert call_args[1]["params"]["noStatusUpdate"] == 1


async def test_talk_list_conversations_with_modified_since(mocker):
    """modifiedSince and includeStatus are forwarded as query params."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={"ocs": {"meta": {"status": "ok"}, "data": []}},
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.list_conversations(modified_since=1700000000, include_status=True)

    params = mock_make_request.call_args[1]["params"]
    assert params["modifiedSince"] == 1700000000
    assert params["includeStatus"] == 1


async def test_talk_get_conversation(mocker):
    """get_conversation parses the OCS-wrapped room object."""
    mock_response = create_mock_talk_room_response(
        conversation_id=42, token="xyz", name="My Room"
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    room = await client.get_conversation("xyz")

    assert isinstance(room, TalkConversation)
    assert room.id == 42
    assert room.token == "xyz"
    assert room.name == "My Room"

    assert mock_make_request.call_args[0][0] == "GET"
    assert "/api/v4/room/xyz" in mock_make_request.call_args[0][1]


async def test_talk_get_conversation_not_found(mocker):
    """A 404 from spreed propagates as HTTPStatusError."""
    error_response = create_mock_error_response(404, "Conversation not found")
    mock_make_request = mocker.patch.object(TalkClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "404 Not Found",
        request=httpx.Request("GET", "http://test.local"),
        response=error_response,
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.get_conversation("nope")
    assert excinfo.value.response.status_code == 404


async def test_talk_create_conversation(mocker):
    """create_conversation builds the right POST body."""
    mock_response = create_mock_talk_room_response(
        conversation_id=10, token="new", name="New Room"
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    room = await client.create_conversation(room_name="New Room")

    assert room.token == "new"
    call_args = mock_make_request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[1]["json"] == {"roomType": 2, "roomName": "New Room"}


async def test_talk_delete_conversation(mocker):
    """delete_conversation issues DELETE on the room URL."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={"ocs": {"meta": {"status": "ok"}, "data": []}},
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.delete_conversation("doomed")

    call_args = mock_make_request.call_args
    assert call_args[0][0] == "DELETE"
    assert "/api/v4/room/doomed" in call_args[0][1]


# Chat tests


async def test_talk_get_messages(mocker):
    """get_messages parses the OCS-wrapped message list."""
    mock_response = create_mock_response(
        status_code=200,
        headers={"X-Chat-Last-Given": "100"},
        json_data={
            "ocs": {
                "meta": {"status": "ok"},
                "data": [
                    {
                        "id": 101,
                        "token": "abc",
                        "actorType": "users",
                        "actorId": "alice",
                        "actorDisplayName": "Alice",
                        "timestamp": 1700000000,
                        "messageType": "comment",
                        "message": "Hi",
                    },
                    {
                        "id": 102,
                        "token": "abc",
                        "actorType": "users",
                        "actorId": "bob",
                        "actorDisplayName": "Bob",
                        "timestamp": 1700000010,
                        "messageType": "comment",
                        "message": "Hello",
                    },
                ],
            }
        },
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    messages, last_given = await client.get_messages("abc", limit=20)

    assert len(messages) == 2
    assert all(isinstance(m, TalkMessage) for m in messages)
    assert messages[0].id == 101
    assert messages[1].actorId == "bob"
    assert last_given == 100

    call_args = mock_make_request.call_args
    assert call_args[0][0] == "GET"
    assert "/chat/abc" in call_args[0][1]
    params = call_args[1]["params"]
    assert params["limit"] == 20
    # Defaults: not look_into_future, not set_read_marker
    assert params["lookIntoFuture"] == 0
    assert params["setReadMarker"] == 0


async def test_talk_get_messages_pagination_cursor(mocker):
    """last_known_message_id is forwarded as the spreed cursor param."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={"ocs": {"meta": {"status": "ok"}, "data": []}},
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    messages, last_given = await client.get_messages(
        "abc", last_known_message_id=500, include_last_known=True
    )

    assert messages == []
    assert last_given is None  # No header on this response

    params = mock_make_request.call_args[1]["params"]
    assert params["lastKnownMessageId"] == 500
    assert params["includeLastKnown"] == 1


async def test_talk_get_messages_invalid_last_given_header(mocker, caplog):
    """A non-numeric X-Chat-Last-Given falls back to None and logs a warning."""
    mock_response = create_mock_response(
        status_code=200,
        headers={"X-Chat-Last-Given": "not-a-number"},
        json_data={"ocs": {"meta": {"status": "ok"}, "data": []}},
    )
    mocker.patch.object(TalkClient, "_make_request", return_value=mock_response)

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with caplog.at_level(logging.WARNING, logger="nextcloud_mcp_server.client.talk"):
        messages, last_given = await client.get_messages("abc")

    assert messages == []
    assert last_given is None
    assert any(
        "Invalid X-Chat-Last-Given" in record.message for record in caplog.records
    ), "Expected a warning log for the malformed header"


async def test_talk_send_message(mocker):
    """send_message posts the message text and parses the response."""
    mock_response = create_mock_talk_message_response(
        message_id=999, token="abc", text="Posted from MCP"
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    posted = await client.send_message("abc", "Posted from MCP")

    assert isinstance(posted, TalkMessage)
    assert posted.id == 999
    assert posted.message == "Posted from MCP"

    call_args = mock_make_request.call_args
    assert call_args[0][0] == "POST"
    assert "/chat/abc" in call_args[0][1]
    body = call_args[1]["json"]
    assert body["message"] == "Posted from MCP"
    # Optional fields are not sent unless explicitly set
    assert "replyTo" not in body
    assert "referenceId" not in body
    assert "silent" not in body


async def test_talk_send_message_with_reference_id_and_reply(mocker):
    """send_message forwards reply_to, reference_id, silent."""
    mock_response = create_mock_talk_message_response(
        message_id=1000, token="abc", text="A reply"
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.send_message(
        "abc",
        "A reply",
        reply_to=999,
        reference_id="dedupe-token",
        silent=True,
    )

    body = mock_make_request.call_args[1]["json"]
    assert body == {
        "message": "A reply",
        "replyTo": 999,
        "referenceId": "dedupe-token",
        "silent": True,
    }


async def test_talk_mark_as_read_no_message(mocker):
    """mark_as_read with no message ID sends no body (json=None)."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={"ocs": {"meta": {"status": "ok"}, "data": []}},
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.mark_as_read("abc")

    call_args = mock_make_request.call_args
    assert call_args[0][0] == "POST"
    assert "/chat/abc/read" in call_args[0][1]
    # Empty body is sent as ``json=None`` so httpx skips both the body and
    # the ``Content-Type: application/json`` header for this bodyless POST.
    assert call_args[1]["json"] is None


async def test_talk_mark_as_read_with_message(mocker):
    """mark_as_read sends lastReadMessage when provided."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={"ocs": {"meta": {"status": "ok"}, "data": []}},
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.mark_as_read("abc", last_read_message=500)

    assert mock_make_request.call_args[1]["json"] == {"lastReadMessage": 500}


# Participant tests


async def test_talk_list_participants(mocker):
    """list_participants parses the OCS-wrapped participant list."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={
            "ocs": {
                "meta": {"status": "ok"},
                "data": [
                    {
                        "attendeeId": 1,
                        "actorType": "users",
                        "actorId": "alice",
                        "displayName": "Alice",
                        "participantType": 1,
                        "inCall": 0,
                        "lastPing": 1700000000,
                    },
                    {
                        "attendeeId": 2,
                        "actorType": "users",
                        "actorId": "bob",
                        "displayName": "Bob",
                        "participantType": 3,
                        "inCall": 0,
                        "lastPing": 1700000000,
                    },
                ],
            }
        },
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    participants = await client.list_participants("abc")

    assert len(participants) == 2
    assert all(isinstance(p, TalkParticipant) for p in participants)
    assert participants[0].actorId == "alice"
    assert participants[1].participantType == 3

    call_args = mock_make_request.call_args
    assert call_args[0][0] == "GET"
    assert "/api/v4/room/abc/participants" in call_args[0][1]
    # Default: include_status off → no includeStatus param
    assert "includeStatus" not in call_args[1].get("params", {})


async def test_talk_list_participants_with_include_status(mocker):
    """include_status=True forwards includeStatus=1 as a query param."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={"ocs": {"meta": {"status": "ok"}, "data": []}},
    )
    mock_make_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=mock_response
    )

    client = TalkClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.list_participants("abc", include_status=True)

    params = mock_make_request.call_args[1]["params"]
    assert params["includeStatus"] == 1


# Participants (write) and reactions


@pytest.mark.parametrize("bad_id", [0, -1, "3", 3.5, True, None])
async def test_reaction_rejects_invalid_message_id(mocker, bad_id):
    """A message id that cannot address a message is refused before the wire.

    ``True`` is in the list on purpose: ``bool`` is an ``int`` subclass, so
    without an explicit check it would be accepted as message id 1.
    """
    client = TalkClient(mocker.AsyncMock(), "testuser")
    mock_request = mocker.patch.object(TalkClient, "_make_request")

    with pytest.raises(ValueError, match="Message ID"):
        await client.list_reactions("abc123", bad_id)

    mock_request.assert_not_called()


@pytest.mark.parametrize("bad_reaction", ["", "   "])
async def test_reaction_rejects_blank_emoji(mocker, bad_reaction):
    """spreed rejects these too, but with a 400 that names no field."""
    client = TalkClient(mocker.AsyncMock(), "testuser")
    mock_request = mocker.patch.object(TalkClient, "_make_request")

    with pytest.raises(ValueError, match="Reaction must not be empty"):
        await client.add_reaction("abc123", 1, bad_reaction)

    mock_request.assert_not_called()


async def test_add_reaction_posts_the_emoji(mocker):
    """POST to the reaction endpoint with the emoji in the body."""
    response = mocker.Mock()
    response.json.return_value = {
        "ocs": {
            "meta": {"statuscode": 201},
            "data": {"🎉": [{"actorType": "users", "actorId": "alice"}]},
        }
    }
    mock_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=response
    )
    client = TalkClient(mocker.AsyncMock(), "testuser")

    reactions = await client.add_reaction("abc123", 42, "🎉")

    assert reactions == {"🎉": [{"actorType": "users", "actorId": "alice"}]}
    args, kwargs = mock_request.call_args
    assert args[0] == "POST"
    assert args[1] == "/ocs/v2.php/apps/spreed/api/v1/reaction/abc123/42"
    assert kwargs["json"] == {"reaction": "🎉"}


async def test_remove_reaction_sends_delete_with_body(mocker):
    """spreed takes the emoji in the body of a DELETE, not the query string."""
    response = mocker.Mock()
    response.json.return_value = {"ocs": {"meta": {"statuscode": 200}, "data": {}}}
    mock_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=response
    )
    client = TalkClient(mocker.AsyncMock(), "testuser")

    assert await client.remove_reaction("abc123", 42, "🎉") == {}

    args, kwargs = mock_request.call_args
    assert args[0] == "DELETE"
    assert kwargs["json"] == {"reaction": "🎉"}


@pytest.mark.parametrize("empty", [{}, [], None])
async def test_reaction_map_normalises_empty_payloads(mocker, empty):
    """ "No reactions" arrives in three shapes across spreed's responses.

    Talk 22 returns ``{}`` for an unreacted message, but PHP serialises empty
    maps as ``[]`` elsewhere in this API and ``null`` shows up on error paths.
    All three have to mean the same thing to the caller.
    """
    response = mocker.Mock()
    response.json.return_value = {"ocs": {"meta": {"statuscode": 200}, "data": empty}}
    mocker.patch.object(TalkClient, "_make_request", return_value=response)
    client = TalkClient(mocker.AsyncMock(), "testuser")

    assert await client.list_reactions("abc123", 1) == {}


async def test_list_reactions_filters_by_emoji(mocker):
    """The optional filter is passed as a query parameter, not a body field."""
    response = mocker.Mock()
    response.json.return_value = {"ocs": {"meta": {"statuscode": 200}, "data": {}}}
    mock_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=response
    )
    client = TalkClient(mocker.AsyncMock(), "testuser")

    await client.list_reactions("abc123", 7, reaction="👍")

    assert mock_request.call_args.kwargs["params"] == {"reaction": "👍"}


async def test_add_participant_posts_participant_and_source(mocker):
    """The participant id and its source both reach the wire."""
    response = mocker.Mock()
    response.json.return_value = {"ocs": {"meta": {"statuscode": 200}, "data": []}}
    mock_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=response
    )
    client = TalkClient(mocker.AsyncMock(), "testuser")

    await client.add_participant("abc123", "bob", source="groups")

    args, kwargs = mock_request.call_args
    assert args[0] == "POST"
    assert args[1] == "/ocs/v2.php/apps/spreed/api/v4/room/abc123/participants"
    assert kwargs["json"] == {"newParticipant": "bob", "source": "groups"}


async def test_add_participant_rejects_path_traversal_token(mocker):
    """Token validation guards the participants path too."""
    client = TalkClient(mocker.AsyncMock(), "testuser")
    mock_request = mocker.patch.object(TalkClient, "_make_request")

    with pytest.raises(ValueError, match="Invalid Talk conversation token"):
        await client.add_participant("../../admin", "bob")

    mock_request.assert_not_called()


async def test_create_one_to_one_conversation_omits_room_name(mocker):
    """A one-to-one room is created with no roomName at all.

    spreed names those after the other participant (verified against Talk
    22.0.17), so sending a blank name is a different request from sending
    none -- the key is omitted rather than emptied.
    """
    response = mocker.Mock()
    response.json.return_value = {
        "ocs": {
            "meta": {"statuscode": 200},
            "data": {
                "id": 7,
                "token": "abc123",
                "type": 1,
                "name": "bob",
                "displayName": "Bob",
            },
        }
    }
    mock_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=response
    )
    client = TalkClient(mocker.AsyncMock(), "testuser")

    await client.create_conversation(room_type=1, invite="bob")

    body = mock_request.call_args.kwargs["json"]
    assert body == {"roomType": 1, "invite": "bob"}
    assert "roomName" not in body


async def test_create_group_conversation_sends_room_name(mocker):
    """The group path is unchanged: the name is still sent."""
    response = mocker.Mock()
    response.json.return_value = {
        "ocs": {
            "meta": {"statuscode": 200},
            "data": {
                "id": 8,
                "token": "def456",
                "type": 2,
                "name": "Team",
                "displayName": "Team",
            },
        }
    }
    mock_request = mocker.patch.object(
        TalkClient, "_make_request", return_value=response
    )
    client = TalkClient(mocker.AsyncMock(), "testuser")

    await client.create_conversation(room_type=2, room_name="Team")

    assert mock_request.call_args.kwargs["json"] == {
        "roomType": 2,
        "roomName": "Team",
    }
