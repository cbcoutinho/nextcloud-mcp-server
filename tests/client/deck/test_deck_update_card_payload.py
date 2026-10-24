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

from nextcloud_mcp_server.client.deck import (
    DeckClient,
    _normalize_color,
    _normalize_duedate,
)
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


async def test_title_only_update_preserves_startdate(mocker):
    """Deck >= 1.18 nulls ``startdate`` when the key is omitted (GH #1530)."""
    mock = _patch_get_then_put(mocker, startdate="2030-05-01T08:00:00+00:00")
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, title="Renamed")

    assert _put_body(mock)["startdate"] == "2030-05-01T08:00:00Z"


async def test_empty_startdate_clears_it(mocker):
    mock = _patch_get_then_put(mocker, startdate="2030-05-01T08:00:00+00:00")
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, startdate="")

    assert _put_body(mock)["startdate"] is None


async def test_color_is_carried_over_and_clearable(mocker):
    """Deck 1.18.x nulls the color when the key is omitted, so it is carried
    over; ``""`` clears it and goes on the wire as ``null``."""
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    mock = _patch_get_then_put(mocker, color="00FF00")
    await client.update_card(1, 1, 1, title="Renamed")
    assert _put_body(mock)["color"] == "00FF00"

    mock = _patch_get_then_put(mocker, color="00FF00")
    await client.update_card(1, 1, 1, color="#ff0000")
    assert _put_body(mock)["color"] == "ff0000"

    mock = _patch_get_then_put(mocker, color="00FF00")
    await client.update_card(1, 1, 1, color="")
    assert _put_body(mock)["color"] is None


async def test_create_card_sends_startdate_and_color(mocker):
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        return_value=create_mock_deck_card_response(),
    )
    client = DeckClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    await client.create_card(
        1, 1, "New", startdate="2030-06-01T14:00:00-04:00", color="#FF0000"
    )

    body = mock_make_request.call_args.kwargs["json"]
    assert body["startdate"] == "2030-06-01T18:00:00Z"
    assert body["color"] == "FF0000"


@pytest.mark.parametrize("bad", ["red", "#", "FF00", "#GG0000"])
def test_invalid_color_is_rejected(bad):
    """Deck stores any string verbatim, so non-hex input is refused up front."""
    with pytest.raises(ValueError, match="6-digit hex"):
        _normalize_color(bad)
