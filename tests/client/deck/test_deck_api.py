import logging

import httpx
import pytest

from nextcloud_mcp_server.client.deck import DeckClient
from nextcloud_mcp_server.models.deck import (
    DeckBoard,
    DeckCard,
    DeckComment,
    DeckLabel,
    DeckStack,
)
from tests.client.conftest import (
    create_mock_deck_board_response,
    create_mock_deck_card_response,
    create_mock_deck_comment_response,
    create_mock_deck_label_response,
    create_mock_deck_stack_response,
    create_mock_error_response,
    create_mock_response,
)

logger = logging.getLogger(__name__)

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


# Board Tests


async def test_deck_get_boards(mocker):
    """Test that get_boards correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200,
        json_data=[
            {
                "id": 1,
                "title": "Board 1",
                "color": "FF0000",
                "owner": {
                    "primaryKey": "testuser",
                    "uid": "testuser",
                    "displayname": "Test User",
                },
                "archived": False,
                "labels": [],
                "acl": [],
                "permissions": {
                    "PERMISSION_READ": True,
                    "PERMISSION_EDIT": True,
                    "PERMISSION_MANAGE": True,
                    "PERMISSION_SHARE": True,
                },
                "users": [],
                "deletedAt": 0,
            },
            {
                "id": 2,
                "title": "Board 2",
                "color": "00FF00",
                "owner": {
                    "primaryKey": "testuser",
                    "uid": "testuser",
                    "displayname": "Test User",
                },
                "archived": False,
                "labels": [],
                "acl": [],
                "permissions": {
                    "PERMISSION_READ": True,
                    "PERMISSION_EDIT": True,
                    "PERMISSION_MANAGE": True,
                    "PERMISSION_SHARE": True,
                },
                "users": [],
                "deletedAt": 0,
            },
        ],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    boards = await client.get_boards()

    assert isinstance(boards, list)
    assert len(boards) == 2
    assert all(isinstance(b, DeckBoard) for b in boards)
    assert boards[0].id == 1
    assert boards[0].title == "Board 1"

    mock_make_request.assert_called_once()


async def test_deck_create_board(mocker):
    """Test that create_board correctly parses the API response."""
    mock_response = create_mock_deck_board_response(
        board_id=123, title="New Board", color="FF0000"
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    board = await client.create_board(title="New Board", color="FF0000")

    assert isinstance(board, DeckBoard)
    assert board.id == 123
    assert board.title == "New Board"
    assert board.color == "FF0000"

    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[1]["json"]["title"] == "New Board"


async def test_deck_get_board(mocker):
    """Test that get_board correctly parses the API response."""
    mock_response = create_mock_deck_board_response(
        board_id=123, title="Test Board", color="0000FF"
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    board = await client.get_board(board_id=123)

    assert isinstance(board, DeckBoard)
    assert board.id == 123
    assert board.title == "Test Board"

    mock_make_request.assert_called_once()
    assert "/boards/123" in mock_make_request.call_args[0][1]


async def test_deck_update_board(mocker):
    """Test that update_board makes the correct API call."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    await client.update_board(board_id=123, title="Updated Board", color="00FF00")

    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args
    assert call_args[0][0] == "PUT"
    assert "/boards/123" in call_args[0][1]
    assert call_args[1]["json"]["title"] == "Updated Board"


async def test_deck_get_board_nonexistent(mocker):
    """Test that getting a non-existent board raises HTTPStatusError."""
    error_response = create_mock_error_response(404, "Board not found")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(DeckClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "404 Not Found",
        request=httpx.Request("GET", "http://test.local"),
        response=error_response,
    )

    client = DeckClient(mock_client, "testuser")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.get_board(board_id=999999999)

    assert excinfo.value.response.status_code == 404


# Stack Tests


async def test_deck_create_stack(mocker):
    """Test that create_stack correctly parses the API response."""
    mock_response = create_mock_deck_stack_response(
        stack_id=456, title="Test Stack", board_id=123, order=1
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    stack = await client.create_stack(board_id=123, title="Test Stack", order=1)

    assert isinstance(stack, DeckStack)
    assert stack.id == 456
    assert stack.title == "Test Stack"
    assert stack.boardId == 123

    mock_make_request.assert_called_once()


async def test_deck_get_stack(mocker):
    """Test that get_stack correctly parses the API response."""
    mock_response = create_mock_deck_stack_response(
        stack_id=456, title="Test Stack", board_id=123, order=1
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    stack = await client.get_stack(board_id=123, stack_id=456)

    assert isinstance(stack, DeckStack)
    assert stack.id == 456
    assert stack.title == "Test Stack"

    mock_make_request.assert_called_once()
    assert "/boards/123/stacks/456" in mock_make_request.call_args[0][1]


async def test_deck_get_stacks(mocker):
    """Test that get_stacks correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200,
        json_data=[
            {"id": 1, "title": "Stack 1", "boardId": 123, "order": 1, "deletedAt": 0},
            {"id": 2, "title": "Stack 2", "boardId": 123, "order": 2, "deletedAt": 0},
        ],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    stacks = await client.get_stacks(board_id=123)

    assert isinstance(stacks, list)
    assert len(stacks) == 2
    assert all(isinstance(s, DeckStack) for s in stacks)

    mock_make_request.assert_called_once()


async def test_deck_get_archived_stacks(mocker):
    """Test that get_archived_stacks targets the archived endpoint and parses the response."""
    mock_response = create_mock_response(
        status_code=200,
        json_data=[
            {
                "id": 9,
                "title": "Archived Stack",
                "boardId": 123,
                "order": 1,
                "deletedAt": 0,
            },
        ],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    stacks = await client.get_archived_stacks(board_id=123)

    assert isinstance(stacks, list)
    assert len(stacks) == 1
    assert stacks[0].id == 9

    mock_make_request.assert_called_once()
    assert "/boards/123/stacks/archived" in mock_make_request.call_args.args[1]


# Card Tests


async def test_deck_create_card(mocker):
    """Test that create_card correctly parses the API response."""
    mock_response = create_mock_deck_card_response(
        card_id=789, title="Test Card", stack_id=456, description="Test description"
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    card = await client.create_card(
        board_id=123, stack_id=456, title="Test Card", description="Test description"
    )

    assert isinstance(card, DeckCard)
    assert card.id == 789
    assert card.title == "Test Card"
    assert card.description == "Test description"

    mock_make_request.assert_called_once()


async def test_deck_get_card(mocker):
    """Test that get_card correctly parses the API response."""
    mock_response = create_mock_deck_card_response(
        card_id=789, title="Test Card", stack_id=456
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    card = await client.get_card(board_id=123, stack_id=456, card_id=789)

    assert isinstance(card, DeckCard)
    assert card.id == 789
    assert card.title == "Test Card"

    mock_make_request.assert_called_once()
    assert "/boards/123/stacks/456/cards/789" in mock_make_request.call_args[0][1]


async def test_deck_assign_dependent_card(mocker):
    """Test that assign_dependent_card POSTs to the right route and parses the card."""
    mock_response = create_mock_deck_card_response(
        card_id=789, title="Test Card", stack_id=456, dependentCards=[555]
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    card = await client.assign_dependent_card(
        board_id=123, stack_id=456, card_id=789, dependent_card_id=555
    )

    assert isinstance(card, DeckCard)
    assert card.dependentCards == [555]

    mock_make_request.assert_called_once()
    assert mock_make_request.call_args[0][0] == "POST"
    assert (
        "/boards/123/stacks/456/cards/789/dependentCards/555"
        in mock_make_request.call_args[0][1]
    )


async def test_deck_remove_dependent_card(mocker):
    """Test that remove_dependent_card DELETEs the right route and parses the card."""
    mock_response = create_mock_deck_card_response(
        card_id=789, title="Test Card", stack_id=456, dependentCards=[]
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    card = await client.remove_dependent_card(
        board_id=123, stack_id=456, card_id=789, dependent_card_id=555
    )

    assert isinstance(card, DeckCard)
    assert card.dependentCards == []

    mock_make_request.assert_called_once()
    assert mock_make_request.call_args[0][0] == "DELETE"
    assert (
        "/boards/123/stacks/456/cards/789/dependentCards/555"
        in mock_make_request.call_args[0][1]
    )


async def test_deck_update_card(mocker):
    """Test that update_card makes the correct API calls."""
    # Mock get_card response (update_card calls get_card first)
    get_response = create_mock_deck_card_response(
        card_id=789, title="Original Card", stack_id=456
    )

    # Mock update response
    update_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(DeckClient, "_make_request")
    # First call returns the card, second call is the update
    mock_make_request.side_effect = [get_response, update_response]

    client = DeckClient(mock_client, "testuser")
    await client.update_card(
        board_id=123, stack_id=456, card_id=789, title="Updated Card"
    )

    # Should be called twice: GET then PUT
    assert mock_make_request.call_count == 2

    # Check the PUT call
    put_call = mock_make_request.call_args_list[1]
    assert put_call[0][0] == "PUT"
    assert "/boards/123/stacks/456/cards/789" in put_call[0][1]
    body = put_call[1]["json"]
    assert body["title"] == "Updated Card"
    # Deck's PUT is a full replacement, so the fields the caller did NOT pass
    # must still be carried over. Asserting only `title` is what let a bug
    # through where order was reset to 0 and the due date cleared on every
    # partial update; see test_deck_update_card_payload.py.
    assert body["order"] == 999  # from the mocked current card
    assert "duedate" in body
    assert body["archived"] is False


# Label Tests


async def test_deck_create_label(mocker):
    """Test that create_label correctly parses the API response."""
    mock_response = create_mock_deck_label_response(
        label_id=111, title="Test Label", color="FF0000", board_id=123
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    label = await client.create_label(board_id=123, title="Test Label", color="FF0000")

    assert isinstance(label, DeckLabel)
    assert label.id == 111
    assert label.title == "Test Label"
    assert label.color == "FF0000"

    mock_make_request.assert_called_once()


async def test_deck_get_label(mocker):
    """Test that get_label correctly parses the API response."""
    mock_response = create_mock_deck_label_response(
        label_id=111, title="Test Label", color="FF0000", board_id=123
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    label = await client.get_label(board_id=123, label_id=111)

    assert isinstance(label, DeckLabel)
    assert label.id == 111
    assert label.title == "Test Label"

    mock_make_request.assert_called_once()
    assert "/boards/123/labels/111" in mock_make_request.call_args[0][1]


# Comment Tests


async def test_deck_create_comment(mocker):
    """Test that create_comment correctly parses the API response (OCS format)."""
    mock_response = create_mock_deck_comment_response(
        comment_id=222, message="Test comment", card_id=789
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    comment = await client.create_comment(card_id=789, message="Test comment")

    assert isinstance(comment, DeckComment)
    assert comment.id == 222
    assert comment.message == "Test comment"

    mock_make_request.assert_called_once()


async def test_deck_get_comments(mocker):
    """Test that get_comments correctly parses the API response (OCS format)."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={
            "ocs": {
                "meta": {"status": "ok"},
                "data": [
                    {
                        "id": 1,
                        "objectId": 789,
                        "message": "Comment 1",
                        "actorId": "testuser",
                        "actorDisplayName": "Test User",
                        "actorType": "users",
                        "creationDateTime": "2024-01-01T00:00:00+00:00",
                        "mentions": [],
                    },
                    {
                        "id": 2,
                        "objectId": 789,
                        "message": "Comment 2",
                        "actorId": "testuser",
                        "actorDisplayName": "Test User",
                        "actorType": "users",
                        "creationDateTime": "2024-01-01T00:00:00+00:00",
                        "mentions": [],
                    },
                ],
            }
        },
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    comments = await client.get_comments(card_id=789)

    assert isinstance(comments, list)
    assert len(comments) == 2
    assert all(isinstance(c, DeckComment) for c in comments)
    assert comments[0].message == "Comment 1"

    mock_make_request.assert_called_once()


async def test_deck_update_comment(mocker):
    """Test that update_comment correctly parses the API response (OCS format)."""
    mock_response = create_mock_deck_comment_response(
        comment_id=222, message="Updated comment", card_id=789
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    comment = await client.update_comment(
        card_id=789, comment_id=222, message="Updated comment"
    )

    assert isinstance(comment, DeckComment)
    assert comment.id == 222
    assert comment.message == "Updated comment"

    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args
    assert call_args[0][0] == "PUT"
    assert "/cards/789/comments/222" in call_args[0][1]
    assert call_args[1]["json"] == {"message": "Updated comment"}


async def test_deck_create_comment_reply(mocker):
    """Test that create_comment forwards parent_id when replying."""
    mock_response = create_mock_deck_comment_response(
        comment_id=333, message="A reply", card_id=789
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    comment = await client.create_comment(card_id=789, message="A reply", parent_id=222)

    assert isinstance(comment, DeckComment)
    assert comment.id == 333

    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args
    assert call_args[0][0] == "POST"
    assert "/cards/789/comments" in call_args[0][1]
    assert call_args[1]["json"] == {"message": "A reply", "parentId": 222}


async def test_deck_create_comment_omits_parent_id_when_none(mocker):
    """Test that create_comment does not send parentId when not given."""
    mock_response = create_mock_deck_comment_response(
        comment_id=444, message="Top-level", card_id=789
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    await client.create_comment(card_id=789, message="Top-level")

    call_args = mock_make_request.call_args
    assert call_args[1]["json"] == {"message": "Top-level"}
    assert "parentId" not in call_args[1]["json"]


async def test_deck_delete_comment(mocker):
    """Test that delete_comment makes the correct API call."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={"ocs": {"meta": {"status": "ok"}, "data": []}},
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    result = await client.delete_comment(card_id=789, comment_id=222)

    assert result is None
    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args
    assert call_args[0][0] == "DELETE"
    assert "/cards/789/comments/222" in call_args[0][1]


async def test_deck_get_comments_pagination(mocker):
    """Test that get_comments forwards limit and offset as query params."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={"ocs": {"meta": {"status": "ok"}, "data": []}},
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    await client.get_comments(card_id=789, limit=50, offset=100)

    call_args = mock_make_request.call_args
    assert call_args[0][0] == "GET"
    assert "/cards/789/comments" in call_args[0][1]
    assert call_args[1]["params"] == {"limit": 50, "offset": 100}


# Config Test


async def test_deck_get_config(mocker):
    """Test that get_config correctly parses the API response (OCS format)."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={
            "ocs": {
                "meta": {"status": "ok"},
                "data": {
                    "calendar": True,
                    "cardDetailsInModal": True,
                    "cardIdBadge": False,
                },
            }
        },
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        DeckClient, "_make_request", return_value=mock_response
    )

    client = DeckClient(mock_client, "testuser")
    config = await client.get_config()

    assert config.calendar is True
    assert config.cardDetailsInModal is True
    assert config.cardIdBadge is False

    mock_make_request.assert_called_once()
