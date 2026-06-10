"""Unit tests for DeckClient.move_card_to_board and the reorder same-board guard.

These mock the HTTP layer to assert request construction without a live server:
- move_card_to_board must send the card ``id`` and target ``stackId`` in the
  body (the route placeholder is ``{cardId}`` but the controller reads ``id``
  from the body), and preserve due/deleted state.
- reorder_card must reject a target stack that is not on the given board before
  issuing the reorder request, steering callers to move_card_to_board.
"""

import httpx
import pytest

from nextcloud_mcp_server.client.deck import DeckClient
from nextcloud_mcp_server.models.deck import DeckCard
from tests.client.conftest import (
    create_mock_deck_card_response,
    create_mock_response,
)

pytestmark = pytest.mark.unit


def _stacks_list_response(stacks: list[dict]) -> httpx.Response:
    """Mock response for get_stacks (a JSON array of stack objects)."""
    return create_mock_response(status_code=200, json_data=stacks)


async def test_move_card_to_board_sends_id_and_target_stack(mocker):
    """The PUT body carries the card id and the destination stack id."""
    get_card_response = create_mock_deck_card_response(
        card_id=42, title="Movable", stack_id=10, description="keep me"
    )
    put_response = create_mock_deck_card_response(
        card_id=42, title="Movable", stack_id=99, description="keep me"
    )

    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        side_effect=[get_card_response, put_response],
    )

    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    moved = await client.move_card_to_board(
        source_board_id=1,
        source_stack_id=10,
        card_id=42,
        target_stack_id=99,
    )

    assert isinstance(moved, DeckCard)
    assert moved.stackId == 99

    # Second call is the update PUT to the internal card route
    put_call = mock_make_request.call_args_list[1]
    method, url = put_call.args[0], put_call.args[1]
    body = put_call.kwargs["json"]
    assert method == "PUT"
    assert url == "/apps/deck/cards/42"
    assert body["id"] == 42
    assert body["stackId"] == 99
    assert body["title"] == "Movable"
    assert body["description"] == "keep me"
    # No live due/deleted state on the source card
    assert body["duedate"] is None
    assert body["deletedAt"] == 0


async def test_move_card_to_board_preserves_duedate(mocker):
    """A due date on the source card is forwarded as an ISO-8601 string."""
    get_card_response = create_mock_deck_card_response(
        card_id=7, stack_id=10, duedate="2030-01-02T03:04:05+00:00"
    )
    put_response = create_mock_deck_card_response(card_id=7, stack_id=99)

    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        side_effect=[get_card_response, put_response],
    )

    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.move_card_to_board(
        source_board_id=1,
        source_stack_id=10,
        card_id=7,
        target_stack_id=99,
    )

    body = mock_make_request.call_args_list[1].kwargs["json"]
    assert body["duedate"] == "2030-01-02T03:04:05+00:00"


async def test_reorder_card_rejects_cross_board_target(mocker):
    """A target stack absent from the board is rejected before any reorder PUT."""
    # get_stacks returns stacks 10 and 11 — target 99 is on another board
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        return_value=_stacks_list_response(
            [
                {"id": 10, "title": "A", "boardId": 1, "order": 1, "deletedAt": 0},
                {"id": 11, "title": "B", "boardId": 1, "order": 2, "deletedAt": 0},
            ]
        ),
    )

    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(ValueError, match="move_card_to_board"):
        await client.reorder_card(
            board_id=1,
            stack_id=10,
            card_id=42,
            order=0,
            target_stack_id=99,
        )

    # Only get_stacks was issued; the reorder PUT was never sent
    mock_make_request.assert_called_once()
    assert "/stacks" in mock_make_request.call_args.args[1]


async def test_reorder_card_allows_same_board_target(mocker):
    """A target stack on the same board passes the guard and issues the PUT."""
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        side_effect=[
            _stacks_list_response(
                [
                    {"id": 10, "title": "A", "boardId": 1, "order": 1, "deletedAt": 0},
                    {"id": 11, "title": "B", "boardId": 1, "order": 2, "deletedAt": 0},
                ]
            ),
            create_mock_response(status_code=200, json_data={}),
        ],
    )

    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.reorder_card(
        board_id=1,
        stack_id=10,
        card_id=42,
        order=0,
        target_stack_id=11,
    )

    assert mock_make_request.call_count == 2
    reorder_call = mock_make_request.call_args_list[1]
    assert reorder_call.args[0] == "PUT"
    assert reorder_call.args[1] == "/apps/deck/cards/42/reorder"
    assert reorder_call.kwargs["json"] == {"order": 0, "stackId": 11}
