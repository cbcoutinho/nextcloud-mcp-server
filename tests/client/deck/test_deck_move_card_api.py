"""Unit tests for DeckClient.move_card_to_board and the reorder same-board guard.

These mock the HTTP layer to assert request construction without a live server:
- move_card_to_board must validate the destination board, then PUT to the
  internal card route with the card id and target stackId in the body (a
  cross-board move the board/stack-scoped route can't do), restoring done state
  afterwards since that route clears it.
- reorder_card must reject a target stack that is not on the given board before
  issuing the reorder request, while skipping the lookup for same-stack reorders.
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


def _stacks_list_response(stack_ids: list[int]) -> httpx.Response:
    """Mock response for get_stacks (a JSON array of stack objects)."""
    return create_mock_response(
        status_code=200,
        json_data=[
            {"id": sid, "title": f"S{sid}", "boardId": 1, "order": i, "deletedAt": 0}
            for i, sid in enumerate(stack_ids)
        ],
    )


async def test_move_card_to_board_sends_id_and_target_stack(mocker):
    """The PUT lands on the internal card route with id + target stack in body."""
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        side_effect=[
            _stacks_list_response([99]),  # destination validation
            create_mock_deck_card_response(  # get_card (source)
                card_id=42, title="Movable", stack_id=10, description="keep me"
            ),
            create_mock_deck_card_response(  # move PUT
                card_id=42, title="Movable", stack_id=99, description="keep me"
            ),
        ],
    )

    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    moved = await client.move_card_to_board(
        source_board_id=1,
        source_stack_id=10,
        card_id=42,
        target_board_id=2,
        target_stack_id=99,
    )

    assert isinstance(moved, DeckCard)
    assert moved.stackId == 99

    put_call = mock_make_request.call_args_list[2]
    method, url = put_call.args[0], put_call.args[1]
    body = put_call.kwargs["json"]
    assert method == "PUT"
    assert url == "/apps/deck/cards/42"
    assert body["id"] == 42
    assert body["stackId"] == 99
    assert body["title"] == "Movable"
    assert body["description"] == "keep me"
    assert body["duedate"] is None
    assert body["deletedAt"] == 0
    # Not-done card: no follow-up done call
    assert mock_make_request.call_count == 3


async def test_move_card_to_board_preserves_duedate(mocker):
    """A due date on the source card is forwarded as an ISO-8601 string."""
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        side_effect=[
            _stacks_list_response([99]),
            create_mock_deck_card_response(
                card_id=7, stack_id=10, duedate="2030-01-02T03:04:05+00:00"
            ),
            create_mock_deck_card_response(card_id=7, stack_id=99),
        ],
    )

    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.move_card_to_board(
        source_board_id=1,
        source_stack_id=10,
        card_id=7,
        target_board_id=2,
        target_stack_id=99,
    )

    body = mock_make_request.call_args_list[2].kwargs["json"]
    assert body["duedate"] == "2030-01-02T03:04:05+00:00"


async def test_move_card_to_board_restores_done_state(mocker):
    """A done card triggers a follow-up PUT to the done endpoint after the move."""
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        side_effect=[
            _stacks_list_response([99]),
            create_mock_deck_card_response(  # source card is done
                card_id=8, stack_id=10, done="2029-12-31T23:59:00+00:00"
            ),
            create_mock_deck_card_response(card_id=8, stack_id=99, done=None),
            create_mock_response(status_code=200, json_data={}),  # done PUT (ignored)
            create_mock_deck_card_response(  # re-fetch after restore
                card_id=8, stack_id=99, done="2031-01-01T00:00:00+00:00"
            ),
        ],
    )

    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.move_card_to_board(
        source_board_id=1,
        source_stack_id=10,
        card_id=8,
        target_board_id=2,
        target_stack_id=99,
    )

    # 4th call restores done on the moved card
    done_call = mock_make_request.call_args_list[3]
    assert done_call.args[0] == "PUT"
    assert done_call.args[1] == "/apps/deck/cards/8/done"
    # The returned card reflects the re-fetched (restored) done state
    assert result.done is not None


async def test_move_card_to_board_done_restore_failure_is_swallowed(mocker):
    """If the post-move done PUT fails, the move still succeeds (best-effort)."""
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        side_effect=[
            _stacks_list_response([99]),
            create_mock_deck_card_response(  # source card is done
                card_id=8, stack_id=10, done="2029-12-31T23:59:00+00:00"
            ),
            create_mock_deck_card_response(card_id=8, stack_id=99, done=None),  # move
            httpx.HTTPStatusError(  # done PUT fails
                "500 Server Error",
                request=httpx.Request("PUT", "https://test.local"),
                response=create_mock_response(status_code=500, json_data={}),
            ),
        ],
    )

    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    # Does not raise — the move already committed
    result = await client.move_card_to_board(
        source_board_id=1,
        source_stack_id=10,
        card_id=8,
        target_board_id=2,
        target_stack_id=99,
    )

    # Returns the moved card from the (successful) move PUT, done unrestored
    assert result.stackId == 99
    assert result.done is None
    # The done restore was attempted (and failed), with no re-fetch after it
    assert mock_make_request.call_count == 4
    assert mock_make_request.call_args_list[3].args[1] == "/apps/deck/cards/8/done"


async def test_move_card_to_board_rejects_stack_not_on_target_board(mocker):
    """A target stack absent from the target board is rejected before any move."""
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        return_value=_stacks_list_response([50, 51]),  # 99 not present
    )

    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(ValueError, match="not a stack on target board"):
        await client.move_card_to_board(
            source_board_id=1,
            source_stack_id=10,
            card_id=42,
            target_board_id=2,
            target_stack_id=99,
        )

    # Only the destination validation ran; no get_card / move PUT
    mock_make_request.assert_called_once()


async def test_reorder_card_rejects_cross_board_target(mocker):
    """A target stack absent from the board is rejected before any reorder PUT."""
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        return_value=_stacks_list_response([10, 11]),  # target 99 on another board
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


async def test_reorder_card_same_stack_skips_lookup(mocker):
    """A same-stack reorder issues only the PUT — no get_stacks round-trip."""
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        return_value=create_mock_response(status_code=200, json_data={}),
    )

    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.reorder_card(
        board_id=1,
        stack_id=10,
        card_id=42,
        order=3,
        target_stack_id=10,  # same stack — pure reorder
    )

    mock_make_request.assert_called_once()
    call = mock_make_request.call_args
    assert call.args[0] == "PUT"
    assert call.args[1] == "/apps/deck/cards/42/reorder"
    assert call.kwargs["json"] == {"order": 3, "stackId": 10}


async def test_reorder_card_allows_same_board_target(mocker):
    """A target stack on the same board passes the guard and issues the PUT."""
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        side_effect=[
            _stacks_list_response([10, 11]),
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
