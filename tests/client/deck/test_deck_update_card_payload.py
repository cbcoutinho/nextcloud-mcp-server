"""Unit tests for the body DeckClient.update_card sends.

Deck's card PUT is a full replacement, so every field the caller did not supply
has to be carried over from the current card. ``order``, ``duedate`` and
``archived`` were previously only sent when explicitly passed, so a title-only
update reset the card's order to 0 and cleared its due date.

These assert the request body directly — the existing integration tests only
checked fields they had just set, which is why the bug survived.
"""

from datetime import datetime, timedelta, timezone

import httpx2
import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st
from nextcloud_client.deck import (
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
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

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
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, order=0)

    assert _put_body(mock)["order"] == 0


async def test_explicit_false_archived_is_sent(mocker):
    """Un-archiving must survive the same falsy-value trap as ``order=0``."""
    mock = _patch_get_then_put(mocker, archived=True)
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, archived=False)

    assert _put_body(mock)["archived"] is False


async def test_card_without_duedate_sends_null(mocker):
    """The key is present with a null value — the shape move_card_to_board uses —
    rather than being omitted."""
    mock = _patch_get_then_put(mocker, duedate=None)
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, title="Renamed")

    body = _put_body(mock)
    assert "duedate" in body
    assert body["duedate"] is None


async def test_explicit_duedate_is_normalized_to_utc(mocker):
    mock = _patch_get_then_put(mocker)
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, duedate="2030-06-01T14:00:00-04:00")

    assert _put_body(mock)["duedate"] == "2030-06-01T18:00:00Z"


async def test_empty_duedate_clears_it(mocker):
    """An empty string means "clear the due date"; it previously went on the wire
    as ``""``, where Deck's behaviour is undefined."""
    mock = _patch_get_then_put(mocker, duedate="2030-06-01T12:00:00+00:00")
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, duedate="")

    assert _put_body(mock)["duedate"] is None


async def test_done_is_only_sent_when_supplied(mocker):
    """``done`` is deliberately not carried over — the internal move route does
    not accept it, so the two routes differ and preserving it blind could
    re-stamp a timestamp."""
    mock = _patch_get_then_put(mocker)
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, title="Renamed")

    assert "done" not in _put_body(mock)


async def test_create_card_normalizes_duedate(mocker):
    mock_make_request = mocker.patch.object(
        DeckClient,
        "_make_request",
        return_value=create_mock_deck_card_response(),
    )
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

    await client.create_card(1, 1, "New", duedate="2030-06-01T14:00:00-04:00")

    body = mock_make_request.call_args.kwargs["json"]
    assert body["duedate"] == "2030-06-01T18:00:00Z"


_instants = st.datetimes(datetime(1900, 1, 2), datetime(2100, 12, 30))


@st.composite
def duedates(draw) -> tuple[str | None, str | None]:
    """A due date as a caller may send it, and what Deck should receive."""
    kind = draw(st.sampled_from(["aware", "naive", "blank", "unparseable"]))
    event(kind)
    if kind == "aware":  # the same instant in UTC, whole seconds, ``Z``
        utc = draw(_instants)
        offset = timedelta(minutes=draw(st.integers(-23 * 60 - 59, 23 * 60 + 59)))
        local = (utc + offset).replace(tzinfo=timezone(offset))
        raw = local.isoformat()
        if not offset and draw(st.booleans()):
            raw = raw.replace("+00:00", "Z")
        return raw, utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    if kind == "naive":  # Deck reads it in its own zone; not ours to guess
        raw = draw(st.one_of(_instants.map(datetime.isoformat), st.dates().map(str)))
        return raw, raw
    if kind == "blank":  # clears the date
        return draw(st.none() | st.text(" \t\n", max_size=3)), None
    # Passed through, never raised on: normalising is not validating.
    raw = draw(st.text(st.characters(codec="utf-8"), min_size=1).filter(_unparseable))
    return raw, raw


def _unparseable(raw: str) -> bool:
    if not raw.strip():
        return False
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return False


@given(duedates())
@example(("2030-06-01T14:00:00-04:00", "2030-06-01T18:00:00Z"))
@example(("2030-06-01T18:00:00.123456+00:00", "2030-06-01T18:00:00Z"))  # no micros
@example(("not a date", "not a date"))
def test_normalize_duedate(case):
    raw, expected = case
    assert _normalize_duedate(raw) == expected
    assert _normalize_duedate(expected) == expected  # idempotent


async def test_title_only_update_preserves_startdate(mocker):
    """Deck >= 1.18 nulls ``startdate`` when the key is omitted (GH #1530)."""
    mock = _patch_get_then_put(mocker, startdate="2030-05-01T08:00:00+00:00")
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, title="Renamed")

    assert _put_body(mock)["startdate"] == "2030-05-01T08:00:00Z"


async def test_empty_startdate_clears_it(mocker):
    mock = _patch_get_then_put(mocker, startdate="2030-05-01T08:00:00+00:00")
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

    await client.update_card(1, 1, 1, startdate="")

    assert _put_body(mock)["startdate"] is None


async def test_color_is_carried_over_and_clearable(mocker):
    """Deck 1.18.x nulls the color when the key is omitted, so it is carried
    over; ``""`` clears it and goes on the wire as ``null``."""
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

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
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")

    await client.create_card(
        1, 1, "New", startdate="2030-06-01T14:00:00-04:00", color="#FF0000"
    )

    body = mock_make_request.call_args.kwargs["json"]
    assert body["startdate"] == "2030-06-01T18:00:00Z"
    assert body["color"] == "FF0000"


_hex = "0123456789abcdefABCDEF"


@st.composite
def bad_colors(draw) -> str:
    """Not six hex digits: wrong length, or one foreign character."""
    if draw(st.booleans()):
        event("wrong length")
        digits = draw(
            st.text(_hex, max_size=5) | st.text(_hex, min_size=7, max_size=12)
        )
    else:
        event("foreign character")
        digits = draw(st.text(_hex, min_size=6, max_size=6))
        at = draw(st.integers(0, 5))
        foreign = draw(st.characters(codec="utf-8", exclude_characters=_hex))
        digits = digits[:at] + foreign + digits[at + 1 :]
    # "" is the documented "clear the color", so a bare empty value gets its "#".
    return (draw(st.sampled_from(["", "#"])) if digits else "#") + digits


@given(bad_colors())
@example("red")
@example("#")
@example("#GG0000")
def test_invalid_color_is_rejected(bad):
    """Deck stores any string verbatim, so non-hex input is refused up front."""
    with pytest.raises(ValueError, match="6-digit hex"):
        _normalize_color(bad)
