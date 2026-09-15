"""Tool-layer tests for Deck comment length handling.

These register the Deck tools on a fresh ``MCPServer`` and invoke each tool's
underlying function directly, mocking the client. They cover the wiring the
splitter's own unit tests cannot: how many POSTs happen, what ``parent_id``
each one carries, what is *not* called on failure, and what the error text
tells an agent to do next.

The decorators (``@require_scopes``, ``@instrument_tool``) are made transparent
by the ``basicauth_mode`` fixture, mirroring
``tests/unit/test_webdav_tools_exclusion.py``.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.models.deck import DeckComment
from nextcloud_mcp_server.server import deck as deck_module
from nextcloud_mcp_server.server.deck import (
    _COMMENT_MAX_LENGTH,
    _MAX_SPLIT_PARTS,
    configure_deck_tools,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path.

    These tests invoke tool functions directly, so there is no transport and no
    verified token; under an OAuth-style mode the decorator would (correctly)
    deny the call, and the outcome would otherwise depend on whatever
    ``MCP_DEPLOYMENT_MODE`` the developer happens to have exported.
    """
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def deck_tools() -> dict:
    """Register the Deck tools on a fresh MCPServer and return them by name."""
    mcp = MCPServer(name="test-deck-tools")
    configure_deck_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


@pytest.fixture
def create_comment(deck_tools):
    """The ``deck_create_card_comment`` implementation, ready to await.

    Resolving the tool here rather than inside each test keeps the dict lookup
    and attribute access out of ``pytest.raises`` blocks, so the only statement
    that can throw in those blocks is the call under test (python:S5778).
    """
    return deck_tools["deck_create_card_comment"].fn


@pytest.fixture
def update_comment(deck_tools):
    return deck_tools["deck_update_card_comment"].fn


@pytest.fixture
def delete_comment(deck_tools):
    return deck_tools["deck_delete_card_comment"].fn


@pytest.fixture
def list_comments(deck_tools):
    return deck_tools["deck_get_card_comments"].fn


@pytest.fixture
def fake_client():
    client = SimpleNamespace()
    client.deck = AsyncMock()
    return client


@pytest.fixture
def patch_get_client(mocker):
    def _install(client):
        async def fake_get_client(ctx):
            return client

        mocker.patch(
            "nextcloud_mcp_server.server.deck.get_client",
            side_effect=fake_get_client,
        )

    return _install


@pytest.fixture
def ctx() -> SimpleNamespace:
    """A minimal Context-shaped object for the tool decorators.

    A fixture rather than a helper call so it can be passed by name: a call
    here would be a second invocation inside every ``pytest.raises`` block,
    leaving it ambiguous which one was expected to raise (python:S5778).
    """
    context = SimpleNamespace()
    context.request_context = SimpleNamespace()
    return context


def make_comment(comment_id: int, message: str, card_id: int = 42) -> DeckComment:
    return DeckComment(
        id=comment_id,
        objectId=card_id,
        message=message,
        actorId="alice",
        actorType="users",
        actorDisplayName="Alice",
        creationDateTime=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc),
        mentions=[],
    )


def http_error(status: int, *, ocs_message: str | None = None) -> httpx.HTTPStatusError:
    """Build the failure the client layer would raise for ``status``."""
    request = httpx.Request("POST", "https://nextcloud.example/ocs/v2.php")
    if ocs_message is not None:
        response = httpx.Response(
            status,
            request=request,
            json={"ocs": {"meta": {"status": "failure", "message": ocs_message}}},
        )
    else:
        # Deck's masked 500 is a bare JSONResponse, not an OCS envelope.
        response = httpx.Response(
            status,
            request=request,
            json={
                "status": status,
                "message": "Internal server error",
                "requestId": "x",
            },
        )
    return httpx.HTTPStatusError("boom", request=request, response=response)


def long_message(parts_wanted: int) -> str:
    """A markdown-ish message that needs roughly ``parts_wanted`` comments."""
    sentence = (
        "The migration landed and every tenant now resolves its collection "
        "through the registry rather than the hardcoded map. "
    )
    return (sentence * 8 * parts_wanted).strip()


# Split ---------------------------------------------------------------------


async def test_split_posts_a_thread_rooted_at_part_one(
    create_comment, fake_client, patch_get_client, ctx
):
    patch_get_client(fake_client)
    fake_client.deck.create_comment.side_effect = [
        make_comment(101, "part 1"),
        make_comment(102, "part 2"),
        make_comment(103, "part 3"),
    ]

    result = await create_comment(
        ctx=ctx, card_id=42, message=long_message(3), overflow="split"
    )

    calls = fake_client.deck.create_comment.await_args_list
    assert len(calls) == 3
    # Part 1 is top-level; the rest hang off it so the card shows one thread.
    assert calls[0].kwargs["parent_id"] is None
    assert [c.kwargs["parent_id"] for c in calls[1:]] == [101, 101]

    assert result.part_count == 3
    assert [c.id for c in result.parts] == [101, 102, 103]
    assert result.parts[0].id == result.comment.id


async def test_split_parts_each_fit_the_limit_and_carry_numbering(
    create_comment, fake_client, patch_get_client, ctx
):
    patch_get_client(fake_client)
    fake_client.deck.create_comment.side_effect = lambda card_id, message, **kw: (
        make_comment(
            100 + len(fake_client.deck.create_comment.await_args_list), message
        )
    )

    await create_comment(ctx=ctx, card_id=42, message=long_message(4), overflow="split")

    posted = [c.args[1] for c in fake_client.deck.create_comment.await_args_list]
    total = len(posted)
    assert all(len(m) <= _COMMENT_MAX_LENGTH for m in posted)
    assert [m[: m.index(")") + 1] for m in posted] == [
        f"({i}/{total})" for i in range(1, total + 1)
    ]


async def test_split_respects_an_explicit_parent_id_for_part_one(
    create_comment, fake_client, patch_get_client, ctx
):
    """Part 1 replies to the caller's comment; parts 2..N reply to part 1."""
    patch_get_client(fake_client)
    fake_client.deck.create_comment.side_effect = [
        make_comment(201, "p1"),
        make_comment(202, "p2"),
        make_comment(203, "p3"),
    ]

    await create_comment(
        ctx=ctx,
        card_id=42,
        message=long_message(3),
        parent_id=77,
        overflow="split",
    )

    calls = fake_client.deck.create_comment.await_args_list
    assert calls[0].kwargs["parent_id"] == 77
    assert [c.kwargs["parent_id"] for c in calls[1:]] == [201, 201]


async def test_split_on_a_short_message_posts_exactly_one_comment(
    create_comment, fake_client, patch_get_client, ctx
):
    """overflow="split" must stay a no-op when the message already fits."""
    patch_get_client(fake_client)
    fake_client.deck.create_comment.return_value = make_comment(1, "short")

    result = await create_comment(
        ctx=ctx, card_id=42, message="short", overflow="split"
    )

    assert fake_client.deck.create_comment.await_count == 1
    assert fake_client.deck.create_comment.await_args.args[1] == "short"
    assert result.parts is None
    assert result.part_count == 1


async def test_message_needing_more_than_the_cap_posts_nothing(
    create_comment, fake_client, patch_get_client, ctx
):
    patch_get_client(fake_client)

    message = long_message(_MAX_SPLIT_PARTS + 4)

    with pytest.raises(ToolError) as excinfo:
        await create_comment(ctx=ctx, card_id=42, message=message, overflow="split")

    assert f"{_MAX_SPLIT_PARTS}-part limit" in str(excinfo.value)
    assert "Nothing was posted" in str(excinfo.value)
    fake_client.deck.create_comment.assert_not_awaited()


# Partial failure -----------------------------------------------------------


async def test_error_mode_does_not_promise_a_split_that_would_be_rejected(
    create_comment, fake_client, patch_get_client, ctx
):
    """The two modes must agree about what is possible.

    Telling the caller to retry with overflow="split" when the split itself
    exceeds the part cap just restarts the guess-and-retry loop this whole
    feature exists to end.
    """
    patch_get_client(fake_client)
    message = long_message(_MAX_SPLIT_PARTS + 5)

    with pytest.raises(ToolError) as excinfo:
        await create_comment(ctx=ctx, card_id=42, message=message)

    text = str(excinfo.value)
    assert "will not work either" in text
    assert f"{_MAX_SPLIT_PARTS}-part limit" in text
    assert "nc_notes_create_note" in text

    # And the split mode rejects the same message, for the same stated reason.
    with pytest.raises(ToolError, match="will not work either"):
        await create_comment(ctx=ctx, card_id=42, message=message, overflow="split")
    fake_client.deck.create_comment.assert_not_awaited()


@pytest.mark.parametrize("overflow", ["error", "split"])
async def test_enormous_message_is_rejected_without_running_the_splitter(
    create_comment, fake_client, patch_get_client, mocker, overflow, ctx
):
    """A multi-megabyte paste must not be fed through the splitter.

    This is the failure path that fires precisely when a caller oversends, so
    it has to stay cheap; the size alone rules a split out.
    """
    patch_get_client(fake_client)
    spy = mocker.spy(deck_module, "split_message")

    with pytest.raises(ToolError, match="will not work either"):
        await create_comment(
            ctx=ctx, card_id=42, message="word " * 400_000, overflow=overflow
        )

    spy.assert_not_called()
    fake_client.deck.create_comment.assert_not_awaited()


async def test_500_on_delete_is_not_blamed_on_message_length(
    delete_comment, fake_client, patch_get_client, ctx
):
    """A delete carries no message, so the length explanation cannot apply."""
    patch_get_client(fake_client)
    fake_client.deck.delete_comment.side_effect = http_error(500)

    with pytest.raises(MCPError) as excinfo:
        await delete_comment(ctx=ctx, card_id=42, comment_id=7)

    text = str(excinfo.value)
    assert "longer than" not in text, f"delete has no message to be too long: {text}"
    assert "may or may not have been modified" not in text
    assert "500" in text


async def test_partial_split_failure_reports_posted_ids_and_does_not_roll_back(
    create_comment, fake_client, patch_get_client, ctx
):
    """The contract that stops an agent double-posting after a mid-thread failure."""
    patch_get_client(fake_client)
    fake_client.deck.create_comment.side_effect = [
        make_comment(301, "p1"),
        make_comment(302, "p2"),
        http_error(403, ocs_message="Not allowed"),
    ]

    message = long_message(3)

    with pytest.raises(MCPError) as excinfo:
        await create_comment(ctx=ctx, card_id=42, message=message, overflow="split")

    message = str(excinfo.value)
    assert "[301, 302]" in message
    assert "failed_part=3" in message
    assert "Do NOT re-send the whole message" in message
    assert "parent_id=301" in message
    # No rollback: deleting posted parts can itself fail and would destroy
    # content a human may already have read.
    fake_client.deck.delete_comment.assert_not_awaited()


async def test_failure_on_the_first_part_reports_that_nothing_was_posted(
    create_comment, fake_client, patch_get_client, ctx
):
    patch_get_client(fake_client)
    fake_client.deck.create_comment.side_effect = http_error(
        500, ocs_message="Server exploded"
    )

    message = long_message(3)

    with pytest.raises(MCPError) as excinfo:
        await create_comment(ctx=ctx, card_id=42, message=message, overflow="split")

    message = str(excinfo.value)
    assert "Nothing was posted" in message
    assert "retrying the whole message is safe" in message
    assert "posted_comment_ids=[]" in message


# Error mode ----------------------------------------------------------------


async def test_error_mode_posts_nothing_and_names_the_remedy(
    create_comment, fake_client, patch_get_client, ctx
):
    patch_get_client(fake_client)
    message = "x" * 3412

    with pytest.raises(ToolError) as excinfo:
        await create_comment(ctx=ctx, card_id=42, message=message)

    text = str(excinfo.value)
    assert "3412 characters" in text
    assert f"{3412 - _COMMENT_MAX_LENGTH} characters over" in text
    assert "Nothing was posted" in text
    assert 'overflow="split"' in text
    fake_client.deck.create_comment.assert_not_awaited()


async def test_error_is_the_default_overflow_mode(
    create_comment, fake_client, patch_get_client, ctx
):
    """Existing callers must keep seeing a failure rather than N new comments."""
    patch_get_client(fake_client)

    message = long_message(3)

    with pytest.raises(ToolError):
        await create_comment(ctx=ctx, card_id=42, message=message)

    fake_client.deck.create_comment.assert_not_awaited()


async def test_message_at_exactly_the_limit_is_posted_unchanged(
    create_comment, fake_client, patch_get_client, ctx
):
    patch_get_client(fake_client)
    message = "x" * _COMMENT_MAX_LENGTH
    fake_client.deck.create_comment.return_value = make_comment(1, message)

    await create_comment(ctx=ctx, card_id=42, message=message)

    assert fake_client.deck.create_comment.await_args.args[1] == message


async def test_limit_plus_trailing_whitespace_is_accepted(
    create_comment, fake_client, patch_get_client, ctx
):
    """Regression: the guard used to measure with a bare ``len()``.

    The server trims before it measures, so this message is legal there and
    used to be rejected here for no reason.
    """
    patch_get_client(fake_client)
    message = "x" * _COMMENT_MAX_LENGTH + "\n\n\n\n"
    fake_client.deck.create_comment.return_value = make_comment(1, message)

    await create_comment(ctx=ctx, card_id=42, message=message)

    fake_client.deck.create_comment.assert_awaited_once()


@pytest.mark.parametrize(
    "message",
    [
        "",
        "   \n\t ",
        # Non-blank to Python's strip(), but PHP's trim() takes NUL, so Deck
        # would store an empty comment and report success.
        "\0",
        # Blank to Python's strip(), non-blank to PHP's: an ideographic space.
        "　",
    ],
)
@pytest.mark.parametrize("overflow", ["error", "split"])
async def test_blank_message_is_rejected_in_both_modes(
    create_comment, fake_client, patch_get_client, message, overflow, ctx
):
    patch_get_client(fake_client)

    with pytest.raises(ToolError, match="empty or whitespace-only"):
        await create_comment(ctx=ctx, card_id=42, message=message, overflow=overflow)

    fake_client.deck.create_comment.assert_not_awaited()


# HTTP error mapping --------------------------------------------------------


async def test_masked_500_on_update_names_the_length_limit(
    update_comment, fake_client, patch_get_client, ctx
):
    """Deck's update endpoint lacks create's MessageTooLongException catch.

    It leaks a masked 500 whose body says only "Internal server error", so the
    mapping has to supply the cause an agent cannot otherwise guess.
    """
    patch_get_client(fake_client)
    fake_client.deck.update_comment.side_effect = http_error(500)

    with pytest.raises(MCPError) as excinfo:
        await update_comment(ctx=ctx, card_id=42, comment_id=7, message="still short")

    text = str(excinfo.value)
    assert str(_COMMENT_MAX_LENGTH) in text
    assert "deck_get_card_comments" in text
    assert "11 characters" in text


async def test_403_on_listing_blames_board_access_not_authorship(
    list_comments, fake_client, patch_get_client, ctx
):
    """A read cannot fail on authorship -- there is nothing being authored.

    The 403 branch is shared by all four comment operations, so it has to say
    something true for each of them; an agent told "only the comment's author
    can edit or delete" after a failed *list* would chase the wrong fix.
    """
    patch_get_client(fake_client)
    fake_client.deck.get_comments.side_effect = http_error(403)

    with pytest.raises(MCPError) as excinfo:
        await list_comments(ctx=ctx, card_id=42)

    text = str(excinfo.value)
    assert "read access" in text
    assert "author" not in text, f"a list has no author to restrict: {text}"
    assert "Nothing was changed" not in text, f"a read changes nothing anyway: {text}"


async def test_403_on_creating_blames_board_write_access_not_authorship(
    create_comment, fake_client, patch_get_client, ctx
):
    """There is no comment yet to be the author of, so it is board access."""
    patch_get_client(fake_client)
    fake_client.deck.create_comment.side_effect = http_error(403)

    with pytest.raises(MCPError) as excinfo:
        await create_comment(ctx=ctx, card_id=42, message="hello")

    text = str(excinfo.value)
    assert "write access to this board" in text
    assert "author" not in text, f"nothing is authored yet on a create: {text}"
    assert "Nothing was posted" in text


async def test_403_on_update_explains_the_author_restriction(
    update_comment, fake_client, patch_get_client, ctx
):
    patch_get_client(fake_client)
    fake_client.deck.update_comment.side_effect = http_error(
        403, ocs_message="Only authors are allowed to edit their comment."
    )

    with pytest.raises(MCPError) as excinfo:
        await update_comment(ctx=ctx, card_id=42, comment_id=7, message="edit")

    text = str(excinfo.value)
    assert "only the comment's author can edit it" in text
    assert "Nothing was changed" in text


async def test_403_on_delete_explains_the_author_restriction(
    delete_comment, fake_client, patch_get_client, ctx
):
    patch_get_client(fake_client)
    fake_client.deck.delete_comment.side_effect = http_error(403)

    with pytest.raises(MCPError) as excinfo:
        await delete_comment(ctx=ctx, card_id=42, comment_id=7)

    text = str(excinfo.value)
    assert "only the comment's author can delete it" in text
    assert "Nothing was deleted" in text


async def test_404_on_listing_comments_is_mapped(
    list_comments, fake_client, patch_get_client, ctx
):
    patch_get_client(fake_client)
    fake_client.deck.get_comments.side_effect = http_error(404)

    with pytest.raises(MCPError, match="does not exist, or you lack access"):
        await list_comments(ctx=ctx, card_id=42)


async def test_400_from_deck_surfaces_the_server_reason(
    create_comment, fake_client, patch_get_client, ctx
):
    """Belt-and-braces: the client-side guard should stop this ever firing."""
    patch_get_client(fake_client)
    fake_client.deck.create_comment.side_effect = http_error(
        400, ocs_message="Message exceeds allowed character limit of 1000"
    )

    with pytest.raises(MCPError) as excinfo:
        await create_comment(ctx=ctx, card_id=42, message="short enough")

    text = str(excinfo.value)
    assert "Message exceeds allowed character limit of 1000" in text
    assert 'overflow="split"' in text


async def test_network_error_is_reported_as_such(
    create_comment, fake_client, patch_get_client, ctx
):
    patch_get_client(fake_client)
    fake_client.deck.create_comment.side_effect = httpx.ConnectError("no route")

    with pytest.raises(MCPError, match="Network error creating a comment"):
        await create_comment(ctx=ctx, card_id=42, message="hello")
