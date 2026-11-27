"""Unit tests for posting over-length Deck comments as threaded parts."""

import httpx2
import pytest
from nextcloud_client.comments import COMMENT_MAX_LENGTH
from nextcloud_client.deck import (
    MAX_SPLIT_COMMENT_PARTS,
    CommentTooLongToSplitError,
    DeckClient,
    PartialSplitCommentError,
    split_comment,
)
from nextcloud_client.models.deck import DeckComment

pytestmark = pytest.mark.unit

SENTENCE = "The migration landed and every tenant now resolves its collection. "


def long_message(parts_wanted: int) -> str:
    return (SENTENCE * 13 * parts_wanted).strip()


def comment(comment_id: int) -> DeckComment:
    return DeckComment.model_validate(
        {
            "id": comment_id,
            "objectId": 42,
            "message": "m",
            "actorId": "alice",
            "actorType": "users",
            "actorDisplayName": "Alice",
            "creationDateTime": "2026-01-01T00:00:00+00:00",
            "mentions": [],
        }
    )


def http_error(status: int) -> httpx2.HTTPStatusError:
    request = httpx2.Request("POST", "https://nc.example/comments")
    response = httpx2.Response(status, request=request)
    return httpx2.HTTPStatusError("boom", request=request, response=response)


@pytest.fixture
def deck(mocker) -> DeckClient:
    client = DeckClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "alice")
    client.create_comment = mocker.AsyncMock()  # type: ignore[method-assign]
    return client


# split_comment --------------------------------------------------------------


def test_short_message_is_one_unprefixed_part():
    assert split_comment("hello") == ["hello"]


def test_long_message_parts_fit_and_are_numbered():
    parts = split_comment(long_message(3))

    assert 1 < len(parts) <= MAX_SPLIT_COMMENT_PARTS
    assert all(len(p) <= COMMENT_MAX_LENGTH for p in parts)
    assert all(p.startswith(f"({i}/{len(parts)}) ") for i, p in enumerate(parts, 1))


def test_blank_message_is_rejected():
    with pytest.raises(ValueError, match="empty or whitespace-only"):
        split_comment(" \n　")


def test_enormous_message_is_rejected_by_size_alone(mocker):
    spy = mocker.patch("nextcloud_client.deck.split_message")
    message = "word " * 400_000

    with pytest.raises(CommentTooLongToSplitError) as excinfo:
        split_comment(message)

    assert excinfo.value.exact is False
    assert excinfo.value.parts_needed == -(-excinfo.value.length // COMMENT_MAX_LENGTH)
    spy.assert_not_called()


def test_message_needing_too_many_parts_reports_the_exact_count():
    # Fits 10 parts by size but not once boundaries and prefixes cost budget.
    message = ("word " * 2000).strip()

    with pytest.raises(CommentTooLongToSplitError) as excinfo:
        split_comment(message)

    assert excinfo.value.exact is True
    assert excinfo.value.parts_needed > MAX_SPLIT_COMMENT_PARTS


# DeckClient.create_split_comment --------------------------------------------


async def test_parts_2_onwards_reply_to_part_1(deck):
    deck.create_comment.side_effect = [comment(301), comment(302), comment(303)]
    parts = split_comment(long_message(3))
    assert len(parts) == 3

    posted = await deck.create_split_comment(42, long_message(3), parent_id=7)

    assert [c.id for c in posted] == [301, 302, 303]
    calls = deck.create_comment.await_args_list
    assert [c.args for c in calls] == [(42, part) for part in parts]
    assert [c.kwargs["parent_id"] for c in calls] == [7, 301, 301]


async def test_short_message_posts_once(deck):
    deck.create_comment.return_value = comment(301)

    posted = await deck.create_split_comment(42, "hello")

    assert [c.id for c in posted] == [301]
    deck.create_comment.assert_awaited_once_with(42, "hello", parent_id=None)


async def test_too_long_posts_nothing(deck):
    with pytest.raises(CommentTooLongToSplitError):
        await deck.create_split_comment(42, long_message(MAX_SPLIT_COMMENT_PARTS + 4))

    deck.create_comment.assert_not_awaited()


async def test_partial_failure_reports_what_was_posted(deck):
    cause = http_error(403)
    deck.create_comment.side_effect = [comment(301), comment(302), cause]

    with pytest.raises(PartialSplitCommentError) as excinfo:
        await deck.create_split_comment(42, long_message(3))

    error = excinfo.value
    assert [c.id for c in error.posted] == [301, 302]
    assert (error.card_id, error.failed_part, error.total_parts) == (42, 3, 3)
    assert error.__cause__ is cause
    assert "[301, 302]" in str(error)


async def test_failure_on_part_1_reports_nothing_posted(deck):
    deck.create_comment.side_effect = httpx2.ConnectError("down")

    with pytest.raises(PartialSplitCommentError) as excinfo:
        await deck.create_split_comment(42, long_message(3))

    assert excinfo.value.posted == []
    assert excinfo.value.failed_part == 1
