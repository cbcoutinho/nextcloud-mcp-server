"""Unit tests for the body DeckClient.update_card sends.

Deck's card PUT is a full replacement, so every field the caller did not supply
has to be carried over from the current card. ``order``, ``duedate`` and
``archived`` were previously only sent when explicitly passed, so a title-only
update reset the card's order to 0 and cleared its due date.

These assert the request body directly — the existing integration tests only
checked fields they had just set, which is why the bug survived.
"""

import httpx
import pytest

from nextcloud_mcp_server.client.deck import DeckClient, _normalize_duedate
from tests.client.conftest import create_mock_deck_card_response, create_mock_response

pytestmark = pytest.mark.unit


def _patch_get_then_put(mocker, **card_fields) -> object:
    """Stub the GET (current card) then the PUT, returning the mock."""
    return mocker.patch.object(
        DeckClient,
        "_make_request",
        side_effect=[
            create_mock_deck_card_response(**card_fields),
            create_mock_response(status_code=200, json_data={}),
        ],
    )


def _put_body(mock_make_request) -> dict:
    """Return the JSON body of the PUT (the second call)."""
    return mock_make_request.call_args_list[1].kwargs["json"]


async def test_title_only_update_preserves_order_and_duedate(mocker):
    """The headline regression: a title-only update must not reset order or
    clear the due date."""
    mock = _patch_get_then_put(
        mocker, order=7, duedate="2030-06-01T12:00:00+00:00", archived=False
    )
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, title="Renamed")

    body = _put_body(mock)
    assert body["title"] == "Renamed"
    assert body["order"] == 7
    assert body["duedate"] == "2030-06-01T12:00:00Z"
    assert body["archived"] is False


async def test_explicit_zero_order_is_sent(mocker):
    """``order=0`` is a legitimate first position.

    Guards against an ``order or current.order`` implementation, which would
    silently discard it.
    """
    mock = _patch_get_then_put(mocker, order=7)
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, order=0)

    assert _put_body(mock)["order"] == 0


async def test_explicit_false_archived_is_sent(mocker):
    """Un-archiving must survive the same falsy-value trap as ``order=0``."""
    mock = _patch_get_then_put(mocker, archived=True)
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, archived=False)

    assert _put_body(mock)["archived"] is False


async def test_card_without_duedate_sends_null(mocker):
    """The key is present with a null value — the shape move_card_to_board uses —
    rather than being omitted."""
    mock = _patch_get_then_put(mocker, duedate=None)
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, title="Renamed")

    body = _put_body(mock)
    assert "duedate" in body
    assert body["duedate"] is None


async def test_explicit_duedate_is_normalized_to_utc(mocker):
    mock = _patch_get_then_put(mocker)
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, duedate="2030-06-01T14:00:00-04:00")

    assert _put_body(mock)["duedate"] == "2030-06-01T18:00:00Z"


async def test_empty_duedate_clears_it(mocker):
    """An empty string means "clear the due date"; it previously went on the wire
    as ``""``, where Deck's behaviour is undefined."""
    mock = _patch_get_then_put(mocker, duedate="2030-06-01T12:00:00+00:00")
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, duedate="")

    assert _put_body(mock)["duedate"] is None


async def test_done_is_only_sent_when_supplied(mocker):
    """``done`` is deliberately not carried over — the internal move route does
    not accept it, so the two routes differ and preserving it blind could
    re-stamp a timestamp."""
    mock = _patch_get_then_put(mocker)
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, title="Renamed")

    assert "done" not in _put_body(mock)


async def test_create_card_normalizes_duedate(mocker):
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        return_value=create_mock_deck_card_response(),
    )
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.create_card(1, 1, "New", duedate="2030-06-01T14:00:00-04:00")

    body = mock_make_request.call_args.kwargs["json"]
    assert body["duedate"] == "2030-06-01T18:00:00Z"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2030-06-01T14:00:00-04:00", "2030-06-01T18:00:00Z"),
        ("2030-06-01T18:00:00Z", "2030-06-01T18:00:00Z"),  # idempotent
        ("2030-06-01T18:00:00+00:00", "2030-06-01T18:00:00Z"),
        ("2030-06-01T18:00:00.123456+00:00", "2030-06-01T18:00:00Z"),  # no micros
        ("2030-06-01T18:00:00", "2030-06-01T18:00:00"),  # naive stays naive
        ("2030-06-01", "2030-06-01"),
        ("not a date", "not a date"),  # passed through, never raises
        ("", None),
        (None, None),
    ],
)
def test_normalize_duedate(raw, expected):
    assert _normalize_duedate(raw) == expected
