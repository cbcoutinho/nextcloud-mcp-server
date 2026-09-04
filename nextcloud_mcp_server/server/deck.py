import logging
from typing import Final, Literal, cast

import anyio
from httpx import HTTPStatusError, RequestError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.capabilities import require_capability
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.links import with_links
from nextcloud_mcp_server.models.deck import (
    AttachFileResponse,
    AttachmentOperationResponse,
    BoardOverviewResponse,
    CardCommentOperationResponse,
    CardCommentResponse,
    CardOperationResponse,
    CreateBoardResponse,
    CreateCardResponse,
    CreateLabelResponse,
    CreateStackResponse,
    DeckAssignedUser,
    DeckBoard,
    DeckCard,
    DeckCardSummary,
    DeckComment,
    DeckCommentSummary,
    DeckLabel,
    DeckStack,
    DeckUser,
    LabelOperationResponse,
    ListAttachmentsResponse,
    ListBoardsResponse,
    ListCardCommentsResponse,
    ListCardsResponse,
    ListLabelsResponse,
    ListStacksResponse,
    StackOperationResponse,
    StackOverview,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.request_context import current_context
from nextcloud_mcp_server.utils.message_splitter import (
    COMMENT_MAX_LENGTH,
    is_blank_comment,
    measured_length,
    split_message,
)

logger = logging.getLogger(__name__)

# Card status filter applied before serialization. "open" (the default for
# list tools) hides archived and explicitly-done cards — the actionable set.
CardStatus = Literal["all", "open", "done", "archived"]
# Per-card detail level. "summary" (the default for list tools) projects each
# card to a compact DeckCardSummary; "full" returns the heavy DeckCard.
DetailLevel = Literal["summary", "full"]
# Default length for the description preview carried in card summaries.
_DEFAULT_DESCRIPTION_PREVIEW = 140


def _validate_positive_length(
    value: int | None, name: str = "description_max_length"
) -> None:
    """Tool-layer guard: reject zero/negative length thresholds.

    Reused for every positive-length knob (description truncation/preview,
    comment message truncation); ``name`` keeps the error message pointed at
    the parameter the caller actually passed.
    """
    if value is not None and value <= 0:
        raise ToolError(f"{name} must be positive, got {value}")


# Nextcloud's core comment cap (see COMMENT_MAX_LENGTH). Deck adds no check of
# its own: its CommentService::create translates the overflow into a 400, but
# its update() has no such catch and leaks a masked 500 (see
# _comment_http_error). Aliased rather than re-declared so the many references
# below -- and their tests -- keep reading the same value as file comments.
_COMMENT_MAX_LENGTH: Final[int] = COMMENT_MAX_LENGTH

# Ceiling on overflow="split". Ten comments is already a wall of text on a card;
# past that the content wants to be a note or a file attachment, not an
# activity-log entry.
_MAX_SPLIT_PARTS: Final[int] = 10

# How a comment overflowing _COMMENT_MAX_LENGTH is handled.
CommentOverflow = Literal["error", "split"]


def _validate_comment_message_not_blank(message: str) -> None:
    """Reject a comment with no content.

    Deck would happily store one, but a blank row in an activity log is noise
    nobody asked for and almost always signals a bug in the caller.

    Blankness is deliberately the *union* of our rule and the server's -- see
    :func:`is_blank_comment` for why neither trim charlist alone is enough.
    """
    if is_blank_comment(message):
        raise ToolError("Comment message must not be empty or whitespace-only")


def _validate_comment_message(message: str) -> None:
    """Reject messages Deck would reject, using the server's own measurement."""
    _validate_comment_message_not_blank(message)

    length = measured_length(message)
    if length > _COMMENT_MAX_LENGTH:
        raise ToolError(_too_long_message(message, length))


def _min_parts_for(length: int) -> int:
    """Lower bound on the parts a message of ``length`` would need.

    Ignores boundaries and the ``(i/N)`` prefix, both of which only ever cost
    budget -- so a real split needs at least this many. That makes it a cheap
    way to rule a split out without running the splitter over a huge message,
    which matters because this runs on the failure path that fires exactly when
    a caller sends far too much.
    """
    return -(-length // _COMMENT_MAX_LENGTH)


def _too_long_to_split_message(length: int, parts_needed: int | None) -> str:
    """Explain a message too long for even the split path to help with.

    Shared by the error mode and the split mode so the two can never disagree:
    an error telling the caller to retry with overflow="split" when the split
    would itself be rejected just restarts the retry loop this all exists to
    end.
    """
    needed = (
        str(parts_needed)
        if parts_needed is not None
        else f"at least {_min_parts_for(length)}"
    )
    return (
        f"Comment message is {length} characters; Nextcloud Deck allows "
        f"{_COMMENT_MAX_LENGTH} per comment. Splitting it would need {needed} "
        f"comments, more than the {_MAX_SPLIT_PARTS}-part limit, so "
        f'overflow="split" will not work either. Nothing was posted. Content '
        f"this long belongs in a note (nc_notes_create_note) or a file "
        f"(nc_webdav_write_file) attached to the card with deck_attach_note / "
        f"deck_attach_file, with a short pointer comment linking to it."
    )


def _too_long_message(message: str, length: int) -> str:
    """Explain an over-length comment in terms an agent can act on directly.

    Deliberately verbose: the failure mode this replaces is an agent retrying
    with a blindly-shortened message over and over, because "too long" alone
    says nothing about how much to cut or that a better option exists. So state
    the exact overage, that nothing was written, and the parameter that fixes
    it -- along with what that parameter will cost.
    """
    # Only promise a split that would actually be accepted. The cheap bound
    # first, so a multi-megabyte paste never reaches the splitter.
    if _min_parts_for(length) > _MAX_SPLIT_PARTS:
        return _too_long_to_split_message(length, None)

    overage = length - _COMMENT_MAX_LENGTH
    # Splitting here purely to quote a part count duplicates the work
    # _post_split_comment does if the caller then retries with overflow="split".
    # That is deliberate: this is a rejection path, the input is bounded to
    # _MAX_SPLIT_PARTS * _COMMENT_MAX_LENGTH by the check above, and telling the
    # agent up front what the remedy costs is the whole point. Don't try to
    # thread a cached result through -- the two calls are far apart and the
    # coupling would cost more than the split.
    parts = split_message(message, max_length=_COMMENT_MAX_LENGTH)
    if len(parts) > _MAX_SPLIT_PARTS:
        return _too_long_to_split_message(length, len(parts))
    return (
        f"Comment message is {length} characters; Nextcloud Deck's limit is "
        f"{_COMMENT_MAX_LENGTH} (measured after trimming whitespace, counting "
        f"Unicode code points). It is {overage} characters over. Nothing was "
        f"posted. Either call deck_create_card_comment again with the SAME "
        f'message and overflow="split" -- it will post {len(parts)} threaded '
        f'comments, each prefixed "(i/{len(parts)})", with parts 2+ replying '
        f"to part 1 -- or shorten the message to {_COMMENT_MAX_LENGTH} "
        f"characters or fewer. Do not retry with a blindly-shortened message: "
        f"the exact overage is {overage} characters."
    )


def _ocs_error_message(e: HTTPStatusError) -> str | None:
    """Pull the human-readable reason out of an OCS error envelope."""
    try:
        meta = e.response.json()["ocs"]["meta"]
    except (ValueError, KeyError, TypeError):
        return None
    message = meta.get("message")
    return message if isinstance(message, str) and message else None


# The four comment operations, kept as a closed set rather than free text so
# each one can carry its own denial reason: a 403 means something different for
# a read than for an edit, and getting that wrong sends an agent after the
# wrong fix.
CommentOperation = Literal["list", "create", "update", "delete"]

_OPERATION_PHRASE: Final[dict[str, str]] = {
    "list": "listing comments on",
    "create": "creating a comment on",
    "update": "updating",
    "delete": "deleting",
}

# Deck's authorship restriction (only the author may edit or delete) applies to
# exactly two of these. For a list it is board read access, and for a create
# there is no comment to be the author of yet.
_FORBIDDEN_REASON: Final[dict[str, str]] = {
    "list": "you lack read access to this board's comments",
    "create": "you lack write access to this board. Nothing was posted",
    "update": (
        "only the comment's author can edit it, and write access to the board "
        "is required. Nothing was changed"
    ),
    "delete": (
        "only the comment's author can delete it, and write access to the "
        "board is required. Nothing was deleted"
    ),
}


def _comment_http_error(
    e: HTTPStatusError,
    *,
    operation: CommentOperation,
    card_id: int,
    comment_id: int | None = None,
    message: str | None = None,
) -> MCPError:
    """Translate a Deck comment API failure into something an agent can act on.

    Args:
        e: The failure raised by the client layer.
        operation: Which comment operation was attempted. Drives both the
            wording and, for 403/500, which explanation actually applies.
        card_id: The card the operation targeted.
        comment_id: The comment targeted, for update/delete.
        message: The text that was being posted, when there was one -- lets the
            masked-500 branch report its measured length.
    """
    status = e.response.status_code
    phrase = _OPERATION_PHRASE[operation]
    target = (
        f"comment {comment_id} on card {card_id}"
        if comment_id is not None
        else f"card {card_id}"
    )

    if status == 400:
        detail = _ocs_error_message(e) or "Deck rejected the request"
        if "character limit" in detail.lower():
            detail += (
                f'. Nothing was posted -- retry with overflow="split" or '
                f"shorten the message to {_COMMENT_MAX_LENGTH} characters"
            )
        reason = f"{detail} (while {phrase} {target})"
    elif status == 403:
        reason = f"Permission denied {phrase} {target}: {_FORBIDDEN_REASON[operation]}."
    elif status == 404:
        reason = (
            f"Not found {phrase} {target}: the card or comment does not "
            f"exist, or you lack access to its board."
        )
    elif status == 429:
        reason = (
            f"Nextcloud rate-limited the request while {phrase} {target} "
            f"(HTTP 429, bruteforce protection). Wait before retrying; when "
            f"splitting, retry only the parts that were not posted."
        )
    elif status == 500 and operation == "update" and message is not None:
        # The length explanation applies only to update: Deck's
        # CommentService::update() lacks the MessageTooLongException catch that
        # create() has, so an over-length message escapes to its
        # ExceptionMiddleware and comes back as a masked, non-OCS 500 with the
        # real cause only in the server log. Name that explicitly -- an agent
        # reading "internal server error" has no way to guess it sent too much.
        # A 500 from any other operation must not be attributed to length, so
        # it falls through to the generic branch below.
        reason = (
            f"Deck returned an unhandled server error (HTTP 500) while "
            f"{phrase} {target}. The most likely cause is a message longer "
            f"than {_COMMENT_MAX_LENGTH} characters (the message you sent is "
            f"{measured_length(message)} characters): unlike the create "
            f"endpoint, Deck's update endpoint does not translate that into a "
            f"400 and leaks a masked 500 instead (upstream Deck bug). The "
            f"comment may or may not have been modified -- call "
            f"deck_get_card_comments to confirm before retrying."
        )
    else:
        reason = (
            f"Deck returned HTTP {status} while {phrase} {target}: "
            f"{_ocs_error_message(e) or e.response.reason_phrase}"
        )

    logger.warning("Deck comment error (%s while %s): %s", status, phrase, reason)
    return MCPError(code=-32603, message=reason)


def _partial_split_error(
    e: HTTPStatusError | RequestError,
    *,
    card_id: int,
    posted: list[DeckComment],
    failed_part: int,
    total_parts: int,
) -> MCPError:
    """Report a split that died partway through.

    No rollback is attempted, deliberately. Deleting the parts already posted
    is itself a write that can 403 or time out, and a half-deleted thread is
    harder to reason about than a clearly-labelled partial one -- it would also
    destroy content a human may already have read. Deck has no transactional
    multi-comment endpoint, so atomicity was never on the table; the honest
    move is to say exactly what exists.

    The bracketed tail is a machine-readable summary for agents that
    pattern-match; the prose ahead of it carries the same facts for those that
    do not.
    """
    posted_ids = [comment.id for comment in posted]
    detail = _ocs_error_message(e) if isinstance(e, HTTPStatusError) else str(e)
    reason = detail or str(e)

    if posted_ids:
        resume = (
            f"To finish, call deck_create_card_comment again with only the "
            f"remaining text and parent_id={posted_ids[0]}, or call "
            f"deck_get_card_comments to inspect the current state first."
        )
        message = (
            f"Split comment partially posted on card {card_id}: parts "
            f"1-{failed_part - 1} of {total_parts} were posted as comment ids "
            f"{posted_ids}. Part {failed_part} failed: {reason}. Parts "
            f"{failed_part}-{total_parts} were NOT posted and no rollback was "
            f"attempted. Do NOT re-send the whole message -- that would "
            f"duplicate parts 1-{failed_part - 1}. {resume} "
            f"[posted_comment_ids={posted_ids} failed_part={failed_part} "
            f"total_parts={total_parts}]"
        )
    else:
        # Part 1 failed, so nothing exists and there is nothing to resume from.
        message = (
            f"Split comment failed on its first part on card {card_id}: "
            f"{reason}. Nothing was posted; retrying the whole message is "
            f"safe. [posted_comment_ids=[] failed_part=1 "
            f"total_parts={total_parts}]"
        )

    logger.warning(
        "Split comment on card %s failed at part %d/%d (posted: %s)",
        card_id,
        failed_part,
        total_parts,
        posted_ids,
    )
    return MCPError(code=-32603, message=message)


async def _post_split_comment(
    client: NextcloudClient, card_id: int, message: str, parent_id: int | None
) -> CardCommentResponse:
    """Post an over-length message as a numbered thread.

    Everything checkable without writing is checked first, so a message that
    cannot be posted never leaves a partial thread behind.
    """
    _validate_comment_message_not_blank(message)
    length = measured_length(message)
    # Cheap bound first, so an enormous paste is rejected without running the
    # splitter over it.
    if _min_parts_for(length) > _MAX_SPLIT_PARTS:
        raise ToolError(_too_long_to_split_message(length, None))

    parts = split_message(message, max_length=_COMMENT_MAX_LENGTH)
    if len(parts) > _MAX_SPLIT_PARTS:
        raise ToolError(_too_long_to_split_message(length, len(parts)))

    posted: list[DeckComment] = []
    for index, part in enumerate(parts, 1):
        # Parts 2..N hang off part 1 so the card renders one thread rather than
        # N unrelated comments.
        reply_to = parent_id if index == 1 else posted[0].id
        try:
            posted.append(
                await client.deck.create_comment(card_id, part, parent_id=reply_to)
            )
        except (HTTPStatusError, RequestError) as e:
            raise _partial_split_error(
                e,
                card_id=card_id,
                posted=posted,
                failed_part=index,
                total_parts=len(parts),
            ) from e

    logger.info(
        "Split a %d-character comment into %d parts on card %s",
        measured_length(message),
        len(posted),
        card_id,
    )
    return CardCommentResponse(comment=posted[0], parts=posted, part_count=len(posted))


def _truncate_card_descriptions(
    cards: list[DeckCard], description_max_length: int | None
) -> None:
    """Truncate descriptions strictly longer than the limit; appends "…" so
    the truncated result is ``description_max_length + 1`` chars."""
    if description_max_length is None:
        return
    for card in cards:
        if card.description and len(card.description) > description_max_length:
            card.description = card.description[:description_max_length] + "…"


def _apply_board_filters(
    board: DeckBoard,
    *,
    include_acl: bool,
    include_users: bool,
    include_labels: bool,
) -> DeckBoard:
    """Drop board sub-fields the caller didn't request; returns the board."""
    if not include_acl:
        board.acl = []
    if not include_users:
        board.users = []
    if not include_labels:
        board.labels = []
    return board


def _extract_uid(user: "DeckUser | DeckAssignedUser") -> str | None:
    """Pull the bare UID out of either assigned-user shape the API returns."""
    if isinstance(user, DeckAssignedUser):
        return user.participant.uid
    if isinstance(user, DeckUser):
        return user.uid
    return None


def _filter_cards(
    cards: list[DeckCard],
    *,
    status: CardStatus,
    label: str | None,
    assigned_to: str | None,
) -> list[DeckCard]:
    """Narrow a flat card list by status/label/assignee before serialization.

    The upstream Deck API returns every card (including archived ones) inline,
    so this filtering reduces the tokens the caller sees but not network
    bandwidth.

    ``open``/``done``/``archived`` partition the cards (no overlap): a card
    that is both done and archived is reported only under ``archived``, since
    archiving is the stronger "off the active board" state.
    """
    if status == "open":
        cards = [c for c in cards if not c.archived and c.done is None]
    elif status == "done":
        cards = [c for c in cards if c.done is not None and not c.archived]
    elif status == "archived":
        cards = [c for c in cards if c.archived]
    # status == "all": no status filter

    if label is not None:
        cards = [
            c for c in cards if any(lbl.title == label for lbl in (c.labels or []))
        ]
    if assigned_to is not None:
        cards = [
            c
            for c in cards
            if assigned_to in {_extract_uid(u) for u in (c.assignedUsers or [])}
        ]
    return cards


def _summarize_card(card: DeckCard, description_preview_length: int) -> DeckCardSummary:
    """Project a full DeckCard down to its compact DeckCardSummary."""
    description = card.description or ""
    has_description = bool(description.strip())
    preview: str | None = None
    if has_description:
        preview = description[:description_preview_length]
        if len(description) > description_preview_length:
            preview += "…"
    assignees = [
        uid for u in (card.assignedUsers or []) if (uid := _extract_uid(u)) is not None
    ]
    return DeckCardSummary(
        id=card.id,
        title=card.title,
        stackId=card.stackId,
        archived=card.archived,
        duedate=card.duedate,
        done=card.done,
        labels=[lbl.title for lbl in (card.labels or [])],
        assignedUsers=assignees,
        attachmentCount=card.attachmentCount,
        commentsUnread=card.commentsUnread,
        hasDescription=has_description,
        descriptionPreview=preview,
    )


def _shape_cards(
    cards: list[DeckCard],
    *,
    detail: DetailLevel,
    status: CardStatus,
    label: str | None,
    assigned_to: str | None,
    description_max_length: int | None,
    description_preview_length: int,
) -> list[DeckCard | DeckCardSummary]:
    """Filter then project a card list according to the requested detail level."""
    filtered = _filter_cards(cards, status=status, label=label, assigned_to=assigned_to)
    if detail == "full":
        _truncate_card_descriptions(filtered, description_max_length)
        return list(filtered)
    return [_summarize_card(c, description_preview_length) for c in filtered]


def _apply_stack_filters(
    stack: DeckStack,
    *,
    include_cards: bool,
    detail: DetailLevel,
    status: CardStatus,
    label: str | None,
    assigned_to: str | None,
    description_max_length: int | None,
    description_preview_length: int,
) -> DeckStack:
    """Apply card filtering + projection to a single stack; returns the stack."""
    if not include_cards:
        stack.cards = None
    elif stack.cards:
        # Cards come straight from the client as DeckCard; the field type is a
        # union only because summary projection writes summaries back into it.
        stack.cards = _shape_cards(
            cast(list[DeckCard], stack.cards),
            detail=detail,
            status=status,
            label=label,
            assigned_to=assigned_to,
            description_max_length=description_max_length,
            description_preview_length=description_preview_length,
        )
    return stack


# Statuses whose result set can contain archived cards. The active Deck
# listing endpoints (get_stacks/get_stack) exclude archived cards at the SQL
# level — only the /stacks/archived endpoint returns them — so these statuses
# need a second fetch and merge. See issue #842.
_ARCHIVED_STATUSES: frozenset[str] = frozenset({"all", "archived"})


async def _archived_cards_by_stack(
    client: NextcloudClient, board_id: int
) -> dict[int, list[DeckCard]]:
    """Map stack_id -> archived DeckCards for a board.

    The active stack/card listing endpoints filter out archived cards in SQL;
    this hits ``/stacks/archived`` (the only endpoint that returns them) and
    keys the cards by their stack so the list tools can merge them back in.
    """
    archived_stacks = await client.deck.get_archived_stacks(board_id)
    return {
        stack.id: cast(list[DeckCard], stack.cards or []) for stack in archived_stacks
    }


def _append_archived_cards(stack: DeckStack, extra: list[DeckCard]) -> None:
    """Append archived cards onto a stack's existing card list, in place.

    Kept separate so the assignment stays correctly typed against
    ``DeckStack.cards`` (``list[DeckCard | DeckCardSummary] | None``).
    """
    merged: list[DeckCard | DeckCardSummary] = list(stack.cards or [])
    merged.extend(extra)
    stack.cards = merged


def _truncate_comment_message(message: str, message_max_length: int | None) -> str:
    """Truncate a comment strictly longer than the limit; appends "…"."""
    if message_max_length is not None and len(message) > message_max_length:
        return message[:message_max_length] + "…"
    return message


def _shape_comments(
    comments: list[DeckComment],
    *,
    detail: DetailLevel,
    message_max_length: int | None,
    order: Literal["newest", "oldest"],
) -> list[DeckComment | DeckCommentSummary]:
    """Order, truncate and (optionally) project a page of card comments."""
    ordered = sorted(
        comments, key=lambda c: c.creationDateTime, reverse=(order == "newest")
    )
    if detail == "full":
        for comment in ordered:
            comment.message = _truncate_comment_message(
                comment.message, message_max_length
            )
        return list(ordered)
    return [
        DeckCommentSummary(
            id=c.id,
            actorId=c.actorId,
            message=_truncate_comment_message(c.message, message_max_length),
            creationDateTime=c.creationDateTime,
        )
        for c in ordered
    ]


# Card attachments — file shares ("Share from Files" picker in the Deck UI).
#
# Mechanism: a Deck card attachment of type="file" is just a Nextcloud share
# with shareType=12 (IShare::TYPE_DECK) and shareWith=<cardId>. The Deck UI
# fires this exact request — see Deck app's
# src/components/card/AttachmentList.vue:223-238 and lib/Service/FilesAppService.php.
# The file is NOT copied; the share row binds the file's existing path to the card.
_SHARE_TYPE_DECK = 12


def _resolve_note_path(notes_folder: str, category: str, title: str) -> str:
    """Reconstruct a note's file path from Notes API metadata.

    Notes are stored as ``<notes_folder>/<category>/<title>.md`` in the
    user's Files; ``<category>`` may be empty or nested (``"Foo/Bar"``).
    """
    parts = [notes_folder.strip("/")]
    if category:
        parts.append(category.strip("/"))
    parts.append(f"{title}.md")
    return "/" + "/".join(p for p in parts if p)


async def _resolve_note_attach_path(client, note_id: int) -> str:
    """Resolve a Notes-app note ID to its filesystem path for sharing.

    Hits the Notes API twice (settings + note metadata) and reconstructs
    the path. Encapsulates the camelCase key (``notesPath``, see
    ``models/notes.py:43``) so a typo there can't silently route to the
    default ``"Notes"`` folder for users who've configured a non-default
    notes location — that bug is exactly what this helper exists to make
    testable.
    """
    async with anyio.create_task_group() as tg:
        settings_holder: list[dict] = []
        note_holder: list[dict] = []

        async def _get_settings() -> None:
            settings_holder.append(await client.notes.get_settings())

        async def _get_note() -> None:
            note_holder.append(await client.notes.get_note(note_id))

        tg.start_soon(_get_settings)
        tg.start_soon(_get_note)

    settings = settings_holder[0]
    note = note_holder[0]
    notes_folder = settings.get("notesPath") or "Notes"
    return _resolve_note_path(
        notes_folder=notes_folder,
        category=note.get("category") or "",
        title=note["title"],
    )


def configure_deck_tools(mcp: MCPServer):
    """Configure Nextcloud Deck tools and resources for the MCP server."""

    # Resources
    @mcp.resource("nc://Deck/boards")
    async def deck_boards_resource():
        """List all Nextcloud Deck boards.

        DEPRECATED: use the ``deck_get_boards`` tool instead.
        """
        ctx = current_context(mcp)
        client = await get_client(ctx)
        boards = await client.deck.get_boards()
        return [board.model_dump() for board in boards]

    @mcp.resource("nc://Deck/boards/{board_id}")
    async def deck_board_resource(board_id: int):
        """Get details of a specific Nextcloud Deck board.

        DEPRECATED: use the ``deck_get_board`` tool instead.
        """
        ctx = current_context(mcp)
        client = await get_client(ctx)
        board = await client.deck.get_board(board_id)
        return board.model_dump()

    @mcp.resource("nc://Deck/boards/{board_id}/stacks")
    async def deck_stacks_resource(board_id: int):
        """List all stacks in a Nextcloud Deck board.

        DEPRECATED: use the ``deck_get_stacks`` tool instead.
        """
        ctx = current_context(mcp)
        client = await get_client(ctx)
        stacks = await client.deck.get_stacks(board_id)
        return [stack.model_dump() for stack in stacks]

    @mcp.resource("nc://Deck/boards/{board_id}/stacks/{stack_id}")
    async def deck_stack_resource(board_id: int, stack_id: int):
        """Get details of a specific Nextcloud Deck stack.

        DEPRECATED: use the ``deck_get_stack`` tool instead.
        """
        ctx = current_context(mcp)
        client = await get_client(ctx)
        stack = await client.deck.get_stack(board_id, stack_id)
        return stack.model_dump()

    @mcp.resource("nc://Deck/boards/{board_id}/stacks/{stack_id}/cards")
    async def deck_cards_resource(board_id: int, stack_id: int):
        """List all cards in a Nextcloud Deck stack.

        DEPRECATED: use the ``deck_get_cards`` tool instead.
        """
        ctx = current_context(mcp)
        client = await get_client(ctx)
        stack = await client.deck.get_stack(board_id, stack_id)
        if stack.cards:
            return [card.model_dump() for card in stack.cards]
        return []

    @mcp.resource("nc://Deck/boards/{board_id}/stacks/{stack_id}/cards/{card_id}")
    async def deck_card_resource(board_id: int, stack_id: int, card_id: int):
        """Get details of a specific Nextcloud Deck card.

        DEPRECATED: use the ``deck_get_card`` tool instead.
        """
        ctx = current_context(mcp)
        client = await get_client(ctx)
        card = await client.deck.get_card(board_id, stack_id, card_id)
        return card.model_dump()

    @mcp.resource("nc://Deck/boards/{board_id}/labels")
    async def deck_labels_resource(board_id: int):
        """List all labels in a Nextcloud Deck board.

        DEPRECATED: use the ``deck_get_labels`` tool instead.
        """
        ctx = current_context(mcp)
        client = await get_client(ctx)
        board = await client.deck.get_board(board_id)
        return [label.model_dump() for label in (board.labels or [])]

    @mcp.resource("nc://Deck/boards/{board_id}/labels/{label_id}")
    async def deck_label_resource(board_id: int, label_id: int):
        """Get details of a specific Nextcloud Deck label.

        DEPRECATED: use the ``deck_get_label`` tool instead.
        """
        ctx = current_context(mcp)
        client = await get_client(ctx)
        label = await client.deck.get_label(board_id, label_id)
        return label.model_dump()

    # Read Tools (converted from resources)

    @mcp.tool(
        title="List Deck Boards",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @with_links
    @instrument_tool
    async def deck_get_boards(ctx: Context) -> ListBoardsResponse:
        """Get all Nextcloud Deck boards"""
        client = await get_client(ctx)
        boards = await client.deck.get_boards()
        return ListBoardsResponse(boards=boards, total=len(boards))

    @mcp.tool(
        title="Get Deck Board",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @with_links
    @instrument_tool
    async def deck_get_board(
        ctx: Context,
        board_id: int,
        include_acl: bool = True,
        include_users: bool = True,
        include_labels: bool = True,
    ) -> DeckBoard:
        """Get details of a specific Nextcloud Deck board.

        Args:
            board_id: The ID of the board
            include_acl: Include the board's ACL entries (default True). Set
                False to reduce response size when ACLs are not needed.
            include_users: Include the board's user list (default True). Set
                False to reduce response size when users are not needed.
            include_labels: Include the board's label definitions (default
                True). Set False to reduce response size. Labels can still be
                retrieved via deck_get_labels.
        """
        client = await get_client(ctx)
        board = await client.deck.get_board(board_id)
        return _apply_board_filters(
            board,
            include_acl=include_acl,
            include_users=include_users,
            include_labels=include_labels,
        )

    @mcp.tool(
        title="List Deck Stacks",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @with_links
    @instrument_tool
    async def deck_get_stacks(
        ctx: Context,
        board_id: int,
        include_cards: bool = True,
        detail: DetailLevel = "summary",
        status: CardStatus = "open",
        label: str | None = None,
        assigned_to: str | None = None,
        description_max_length: int | None = None,
        description_preview_length: int = _DEFAULT_DESCRIPTION_PREVIEW,
    ) -> ListStacksResponse:
        """Get all stacks in a Nextcloud Deck board.

        Cards are returned as compact summaries by default to keep the
        response small on large boards. Filtering/projection happen
        client-side after the API returns the full board, so they reduce the
        tokens the caller sees but not network bandwidth.

        Args:
            board_id: The ID of the board
            include_cards: Include cards inside each stack (default True). Set
                False for a lightweight stack listing. Fetch cards separately
                via deck_get_cards.
            detail: "summary" (default) returns compact card rows. "full"
                returns the complete card objects (the old behavior).
            status: Which cards to include — "open" (default), "done",
                "archived", or "all". The first three partition the board
                (a card that is both done and archived counts as "archived").
                "archived"/"all" include archived cards, which the active
                listing endpoint omits — this costs one extra API call.
            label: If set, only cards carrying a label with this exact title.
            assigned_to: If set, only cards assigned to this user UID.
            description_max_length: In detail="full", truncate each card's
                description to this many characters.
            description_preview_length: In detail="summary", length of the
                description preview carried on each card (default 140).
        """
        _validate_positive_length(description_max_length)
        _validate_positive_length(
            description_preview_length, "description_preview_length"
        )
        client = await get_client(ctx)

        # Fetch active stacks and (when archived cards are in scope) the
        # archived endpoint concurrently, then merge archived cards onto each
        # stack by id before filtering. The active endpoint omits archived
        # cards, so without this merge status="archived"/"all" would drop them.
        stacks_holder: list[list[DeckStack]] = []
        archived_by_stack: dict[int, list[DeckCard]] = {}
        merge_archived = include_cards and status in _ARCHIVED_STATUSES

        async def _get_active() -> None:
            stacks_holder.append(await client.deck.get_stacks(board_id))

        async def _get_archived() -> None:
            archived_by_stack.update(await _archived_cards_by_stack(client, board_id))

        async with anyio.create_task_group() as tg:
            tg.start_soon(_get_active)
            if merge_archived:
                tg.start_soon(_get_archived)

        stacks = stacks_holder[0]
        if merge_archived:
            for stack in stacks:
                extra = archived_by_stack.get(stack.id)
                if extra:
                    _append_archived_cards(stack, extra)

        stacks = [
            _apply_stack_filters(
                stack,
                include_cards=include_cards,
                detail=detail,
                status=status,
                label=label,
                assigned_to=assigned_to,
                description_max_length=description_max_length,
                description_preview_length=description_preview_length,
            )
            for stack in stacks
        ]
        return ListStacksResponse(stacks=stacks, total=len(stacks))

    @mcp.tool(
        title="Get Deck Stack",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @with_links
    @instrument_tool
    async def deck_get_stack(
        ctx: Context,
        board_id: int,
        stack_id: int,
        include_cards: bool = True,
        detail: DetailLevel = "summary",
        status: CardStatus = "open",
        label: str | None = None,
        assigned_to: str | None = None,
        description_max_length: int | None = None,
        description_preview_length: int = _DEFAULT_DESCRIPTION_PREVIEW,
    ) -> DeckStack:
        """Get details of a specific Nextcloud Deck stack.

        Cards are returned as compact summaries by default. See
        deck_get_stacks for the shared parameter semantics.

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            include_cards: Include cards in the stack (default True).
            detail: "summary" (default) or "full".
            status: "open" (default), "done", "archived", or "all"
                (non-overlapping, a done+archived card counts as "archived").
                "archived"/"all" include archived cards, which the active
                listing endpoint omits — this costs one extra API call.
            label: If set, only cards carrying a label with this exact title.
            assigned_to: If set, only cards assigned to this user UID.
            description_max_length: In detail="full", truncate descriptions.
            description_preview_length: In detail="summary", preview length.
        """
        _validate_positive_length(description_max_length)
        _validate_positive_length(
            description_preview_length, "description_preview_length"
        )
        client = await get_client(ctx)
        if status == "archived" and include_cards:
            # Archived-only: the /stacks/archived endpoint already returns the
            # stack (metadata + archived cards) in one call, so skip the active
            # fetch whose open cards would all be filtered out anyway.
            archived = await client.deck.get_archived_stacks(board_id)
            stack = next((s for s in archived if s.id == stack_id), None)
            if stack is None:
                # findAllArchived returns every stack, so this is defensive;
                # fall back to the active endpoint for the stack metadata.
                stack = await client.deck.get_stack(board_id, stack_id)
        else:
            # Active stack always needed (for metadata + open cards); fetch the
            # archived cards concurrently when status="all" needs both sets.
            stack_holder: list[DeckStack] = []
            archived_by_stack: dict[int, list[DeckCard]] = {}
            merge_archived = include_cards and status == "all"

            async def _get_active() -> None:
                stack_holder.append(await client.deck.get_stack(board_id, stack_id))

            async def _get_archived() -> None:
                archived_by_stack.update(
                    await _archived_cards_by_stack(client, board_id)
                )

            async with anyio.create_task_group() as tg:
                tg.start_soon(_get_active)
                if merge_archived:
                    tg.start_soon(_get_archived)

            stack = stack_holder[0]
            extra = archived_by_stack.get(stack_id)
            if extra:
                _append_archived_cards(stack, extra)
        return _apply_stack_filters(
            stack,
            include_cards=include_cards,
            detail=detail,
            status=status,
            label=label,
            assigned_to=assigned_to,
            description_max_length=description_max_length,
            description_preview_length=description_preview_length,
        )

    @mcp.tool(
        title="List Archived Deck Stacks",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @with_links
    @instrument_tool
    async def deck_get_archived_stacks(
        ctx: Context,
        board_id: int,
        detail: DetailLevel = "summary",
        label: str | None = None,
        assigned_to: str | None = None,
        description_max_length: int | None = None,
        description_preview_length: int = _DEFAULT_DESCRIPTION_PREVIEW,
    ) -> ListStacksResponse:
        """List archived stacks (with their archived cards) for a Nextcloud
        Deck board.

        This is the archived-only shortcut: it returns *only* archived cards
        in a single call. The active list tools (deck_get_cards,
        deck_get_stacks, deck_get_board_overview) also include archived cards
        when called with status="archived"/"all". Use this tool when you want
        archived cards exclusively and don't need the open ones. Typical use:
        auditing completed work archived off the active board (e.g. cards moved
        through a "Done" stack and then archived via deck_archive_card). The
        shape mirrors deck_get_stacks.

        Cards are always included on the returned stacks (an archived stack
        without its cards would have no audit value) and returned as compact
        summaries by default. There is no ``status`` filter — every card here
        is archived by definition — but ``label``/``assigned_to`` narrow the
        set just like the active-stack tools.

        Args:
            board_id: The ID of the board
            detail: "summary" (default) or "full".
            label: If set, only cards carrying a label with this exact title.
            assigned_to: If set, only cards assigned to this user UID.
            description_max_length: In detail="full", truncate descriptions.
            description_preview_length: In detail="summary", preview length.
        """
        _validate_positive_length(description_max_length)
        _validate_positive_length(
            description_preview_length, "description_preview_length"
        )
        client = await get_client(ctx)
        stacks = await client.deck.get_archived_stacks(board_id)
        # All cards in archived stacks are themselves archived; status="all"
        # keeps them (an "open"/"done" filter would drop the whole point).
        # label/assigned_to still apply for targeted audits.
        stacks = [
            _apply_stack_filters(
                stack,
                include_cards=True,
                detail=detail,
                status="all",
                label=label,
                assigned_to=assigned_to,
                description_max_length=description_max_length,
                description_preview_length=description_preview_length,
            )
            for stack in stacks
        ]
        return ListStacksResponse(stacks=stacks, total=len(stacks))

    @mcp.tool(
        title="List Deck Cards",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @with_links
    @instrument_tool
    async def deck_get_cards(
        ctx: Context,
        board_id: int,
        stack_id: int,
        detail: DetailLevel = "summary",
        status: CardStatus = "open",
        label: str | None = None,
        assigned_to: str | None = None,
        description_max_length: int | None = None,
        description_preview_length: int = _DEFAULT_DESCRIPTION_PREVIEW,
    ) -> ListCardsResponse:
        """Get all cards in a Nextcloud Deck stack.

        Cards are returned as compact summaries by default. Filtering and
        projection are applied client-side after the API returns the full
        stack, so they reduce the tokens the caller sees but not network
        bandwidth — network-wise this tool is equivalent to
        deck_get_stack(include_cards=True).

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            detail: "summary" (default) returns compact card rows. "full"
                returns the complete card objects.
            status: "open" (default), "done", "archived", or "all". The first
                three partition the board (a done+archived card counts as
                "archived"). "archived"/"all" include archived cards, which the
                active listing endpoint omits — this costs one extra API call.
            label: If set, only cards carrying a label with this exact title.
            assigned_to: If set, only cards assigned to this user UID.
            description_max_length: In detail="full", truncate descriptions.
            description_preview_length: In detail="summary", preview length.
        """
        _validate_positive_length(description_max_length)
        _validate_positive_length(
            description_preview_length, "description_preview_length"
        )
        client = await get_client(ctx)

        # Archived cards are excluded by the active stack endpoint, so for
        # statuses that can include them we also fetch /stacks/archived and
        # merge. "open"/"done" need only the active stack (no extra call).
        active_cards: list[DeckCard] = []
        archived_cards: list[DeckCard] = []
        need_active = status != "archived"
        need_archived = status in _ARCHIVED_STATUSES

        async def _get_active() -> None:
            stack = await client.deck.get_stack(board_id, stack_id)
            active_cards.extend(cast(list[DeckCard], stack.cards or []))

        async def _get_archived() -> None:
            by_stack = await _archived_cards_by_stack(client, board_id)
            archived_cards.extend(by_stack.get(stack_id, []))

        async with anyio.create_task_group() as tg:
            if need_active:
                tg.start_soon(_get_active)
            if need_archived:
                tg.start_soon(_get_archived)

        cards = _shape_cards(
            active_cards + archived_cards,
            detail=detail,
            status=status,
            label=label,
            assigned_to=assigned_to,
            description_max_length=description_max_length,
            description_preview_length=description_preview_length,
        )
        return ListCardsResponse(cards=cards, total=len(cards))

    @mcp.tool(
        title="Get Deck Board Overview",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @with_links
    @instrument_tool
    async def deck_get_board_overview(
        ctx: Context,
        board_id: int,
        status: CardStatus = "open",
        label: str | None = None,
        assigned_to: str | None = None,
        description_preview_length: int = _DEFAULT_DESCRIPTION_PREVIEW,
    ) -> BoardOverviewResponse:
        """Get a compact, whole-board snapshot in a single call.

        Returns the board title, its label legend, and every stack with its
        cards projected to compact summary rows. Prefer it for "show me the
        board" / "what's in progress" style requests on large boards — it is
        the token-efficient way to view board *state*. It intentionally omits
        the board-management fields (ACL, user list, full label objects) that
        deck_get_board exposes. Reach for deck_get_board when you need those.

        Args:
            board_id: The ID of the board
            status: Which cards to include — "open" (default), "done",
                "archived", or "all". The first three partition the board
                (a card that is both done and archived counts as "archived").
                "archived"/"all" include archived cards, which the active
                listing endpoint omits — this costs one extra API call.
            label: If set, only cards carrying a label with this exact title.
            assigned_to: If set, only cards assigned to this user UID.
            description_preview_length: Length of the description preview
                carried on each card summary (default 140).
        """
        _validate_positive_length(
            description_preview_length, "description_preview_length"
        )
        client = await get_client(ctx)

        board_holder: list[DeckBoard] = []
        stacks_holder: list[list[DeckStack]] = []
        archived_by_stack: dict[int, list[DeckCard]] = {}
        merge_archived = status in _ARCHIVED_STATUSES

        async def _get_board() -> None:
            board_holder.append(await client.deck.get_board(board_id))

        async def _get_stacks() -> None:
            stacks_holder.append(await client.deck.get_stacks(board_id))

        async def _get_archived() -> None:
            archived_by_stack.update(await _archived_cards_by_stack(client, board_id))

        async with anyio.create_task_group() as tg:
            tg.start_soon(_get_board)
            tg.start_soon(_get_stacks)
            if merge_archived:
                tg.start_soon(_get_archived)

        board = board_holder[0]
        stacks = stacks_holder[0]

        stack_overviews: list[StackOverview] = []
        total_cards = 0
        for stack in stacks:
            cards = cast(list[DeckCard], stack.cards or [])
            if merge_archived:
                cards = cards + archived_by_stack.get(stack.id, [])
            summaries = [
                _summarize_card(c, description_preview_length)
                for c in _filter_cards(
                    cards,
                    status=status,
                    label=label,
                    assigned_to=assigned_to,
                )
            ]
            total_cards += len(summaries)
            stack_overviews.append(
                StackOverview(
                    id=stack.id,
                    title=stack.title,
                    order=stack.order,
                    card_count=len(summaries),
                    cards=summaries,
                )
            )

        return BoardOverviewResponse(
            board_id=board.id,
            title=board.title,
            labels=[lbl.title for lbl in (board.labels or [])],
            stacks=stack_overviews,
            total_cards=total_cards,
        )

    @mcp.tool(
        title="Get Deck Card",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @with_links
    @instrument_tool
    async def deck_get_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> DeckCard:
        """Get details of a specific Nextcloud Deck card"""
        client = await get_client(ctx)
        card = await client.deck.get_card(board_id, stack_id, card_id)
        return card

    @mcp.tool(
        title="List Deck Labels",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_labels(ctx: Context, board_id: int) -> ListLabelsResponse:
        """Get all labels in a Nextcloud Deck board"""
        client = await get_client(ctx)
        board = await client.deck.get_board(board_id)
        labels = board.labels or []
        return ListLabelsResponse(labels=labels, total=len(labels))

    @mcp.tool(
        title="Get Deck Label",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_label(ctx: Context, board_id: int, label_id: int) -> DeckLabel:
        """Get details of a specific Nextcloud Deck label"""
        client = await get_client(ctx)
        label = await client.deck.get_label(board_id, label_id)
        return label

    # Create/Update/Delete Tools

    @mcp.tool(
        title="Create Deck Board",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_create_board(
        ctx: Context, title: str, color: str
    ) -> CreateBoardResponse:
        """Create a new Nextcloud Deck board

        Args:
            title: The title of the new board
            color: The hexadecimal color of the new board (e.g. FF0000)
        """
        client = await get_client(ctx)
        board = await client.deck.create_board(title, color)
        return CreateBoardResponse(id=board.id, title=board.title, color=board.color)

    # Stack Tools

    @mcp.tool(
        title="Create Deck Stack",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_create_stack(
        ctx: Context, board_id: int, title: str, order: int
    ) -> CreateStackResponse:
        """Create a new stack in a Nextcloud Deck board

        Args:
            board_id: The ID of the board
            title: The title of the new stack
            order: Order for sorting the stacks
        """
        client = await get_client(ctx)
        stack = await client.deck.create_stack(board_id, title, order)
        return CreateStackResponse(id=stack.id, title=stack.title, order=stack.order)

    @mcp.tool(
        title="Update Deck Stack",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_update_stack(
        ctx: Context,
        board_id: int,
        stack_id: int,
        title: str | None = None,
        order: int | None = None,
    ) -> StackOperationResponse:
        """Update a Nextcloud Deck stack

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            title: New title for the stack
            order: New order for the stack
        """
        client = await get_client(ctx)
        await client.deck.update_stack(board_id, stack_id, title, order)
        return StackOperationResponse(
            success=True,
            message="Stack updated successfully",
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Delete Deck Stack",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_delete_stack(
        ctx: Context, board_id: int, stack_id: int
    ) -> StackOperationResponse:
        """Delete a Nextcloud Deck stack

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
        """
        client = await get_client(ctx)
        await client.deck.delete_stack(board_id, stack_id)
        return StackOperationResponse(
            success=True,
            message="Stack deleted successfully",
            stack_id=stack_id,
            board_id=board_id,
        )

    # Card Tools
    @mcp.tool(
        title="Create Deck Card",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @with_links
    @instrument_tool
    async def deck_create_card(
        ctx: Context,
        board_id: int,
        stack_id: int,
        title: str,
        type: str = "plain",
        order: int = 999,
        description: str | None = None,
        duedate: str | None = None,
    ) -> CreateCardResponse:
        """Create a new card in a Nextcloud Deck stack

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            title: The title of the new card
            type: Type of the card (default: plain)
            order: Order for sorting the cards
            description: Description of the card
            duedate: Due date of the card (ISO-8601 format)
        """
        client = await get_client(ctx)
        card = await client.deck.create_card(
            board_id, stack_id, title, type, order, description, duedate
        )
        return CreateCardResponse(
            id=card.id,
            title=card.title,
            stackId=card.stackId,
        )

    @mcp.tool(
        title="Update Deck Card",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @with_links
    @instrument_tool
    async def deck_update_card(
        ctx: Context,
        board_id: int,
        stack_id: int,
        card_id: int,
        title: str | None = None,
        description: str | None = None,
        type: str | None = None,
        owner: str | None = None,
        order: int | None = None,
        duedate: str | None = None,
        archived: bool | None = None,
        done: str | None = None,
    ) -> CardOperationResponse:
        """Update a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            title: New title for the card
            description: New description for the card
            type: New type for the card
            owner: New owner for the card
            order: New order for the card
            duedate: New due date for the card (ISO-8601 format)
            archived: Whether the card should be archived
            done: Completion date for the card (ISO-8601 format)
        """
        client = await get_client(ctx)
        await client.deck.update_card(
            board_id,
            stack_id,
            card_id,
            title,
            description,
            type,
            owner,
            order,
            duedate,
            archived,
            done,
        )
        return CardOperationResponse(
            success=True,
            message="Card updated successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Delete Deck Card",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("deck.write")
    # No @with_links here, deliberately: the card is gone by the time this
    # returns, so a link to it would 404. Every other CardOperationResponse tool
    # leaves the card in place and does carry one. Asserted by
    # tests/unit/test_links_tool_coverage.py.
    @instrument_tool
    async def deck_delete_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> CardOperationResponse:
        """Delete a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = await get_client(ctx)
        await client.deck.delete_card(board_id, stack_id, card_id)
        return CardOperationResponse(
            success=True,
            message="Card deleted successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Archive Deck Card",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @with_links
    @instrument_tool
    async def deck_archive_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> CardOperationResponse:
        """Archive a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = await get_client(ctx)
        await client.deck.archive_card(board_id, stack_id, card_id)
        return CardOperationResponse(
            success=True,
            message="Card archived successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Unarchive Deck Card",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @with_links
    @instrument_tool
    async def deck_unarchive_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> CardOperationResponse:
        """Unarchive a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = await get_client(ctx)
        await client.deck.unarchive_card(board_id, stack_id, card_id)
        return CardOperationResponse(
            success=True,
            message="Card unarchived successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Reorder Deck Card",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @with_links
    @instrument_tool
    async def deck_reorder_card(
        ctx: Context,
        board_id: int,
        stack_id: int,
        card_id: int,
        order: int,
        target_stack_id: int,
    ) -> CardOperationResponse:
        """Reorder a Nextcloud Deck card within a board.

        Moves a card to a new position, optionally into a different stack on
        the SAME board. To move a card to a stack on a DIFFERENT board, use
        deck_move_card_to_board instead — reordering across boards is rejected
        because it would orphan the card's board-scoped labels.

        Args:
            board_id: The ID of the board
            stack_id: The ID of the current stack
            card_id: The ID of the card
            order: New position in the target stack
            target_stack_id: The ID of the target stack (must be on board_id)
        """
        client = await get_client(ctx)
        await client.deck.reorder_card(
            board_id, stack_id, card_id, order, target_stack_id
        )
        return CardOperationResponse(
            success=True,
            message="Card reordered successfully",
            card_id=card_id,
            stack_id=target_stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Move Deck Card to Another Board",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @with_links
    @instrument_tool
    async def deck_move_card_to_board(
        ctx: Context,
        source_board_id: int,
        source_stack_id: int,
        card_id: int,
        target_board_id: int,
        target_stack_id: int,
        order: int = 0,
    ) -> CardOperationResponse:
        """Move a Nextcloud Deck card to a stack on a different board.

        The card keeps its identity (same id, comments, attachments), along
        with its archived state, due date and user assignments (an assignee
        without access to the target board stays assigned but cannot act on the
        card). Deck remaps the card's board-scoped labels to the destination
        board by title — reusing a same-titled label there, or cloning it when
        you have board-manage permission. Use deck_reorder_card for moves
        within a single board.

        Two caveats from Deck's move route: the card's owner is reassigned to
        the user performing the move (the original owner is not preserved), and
        a card marked done keeps its done state but its done timestamp is reset
        to the time of the move.

        target_stack_id must be a stack on target_board_id. The move is
        rejected otherwise.

        Args:
            source_board_id: The ID of the board the card currently lives on
            source_stack_id: The ID of the stack the card currently lives in
            card_id: The ID of the card to move
            target_board_id: The ID of the destination board
            target_stack_id: The ID of the destination stack (must be on target_board_id)
            order: Position within the destination stack (default 0 = top)
        """
        client = await get_client(ctx)
        moved = await client.deck.move_card_to_board(
            source_board_id,
            source_stack_id,
            card_id,
            target_board_id,
            target_stack_id,
            order,
        )
        # Surface the post-move labels so callers can confirm the remap without
        # a follow-up get_card (label remapping is this tool's whole point).
        return CardOperationResponse(
            success=True,
            message="Card moved to board successfully",
            card_id=card_id,
            stack_id=target_stack_id,
            board_id=target_board_id,
            labels=[label.title for label in (moved.labels or [])],
        )

    # Label Tools
    @mcp.tool(
        title="Create Deck Label",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_create_label(
        ctx: Context, board_id: int, title: str, color: str
    ) -> CreateLabelResponse:
        """Create a new label in a Nextcloud Deck board

        Args:
            board_id: The ID of the board
            title: The title of the new label
            color: The color of the new label (hex format without #)
        """
        client = await get_client(ctx)
        label = await client.deck.create_label(board_id, title, color)
        return CreateLabelResponse(id=label.id, title=label.title, color=label.color)

    @mcp.tool(
        title="Update Deck Label",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_update_label(
        ctx: Context,
        board_id: int,
        label_id: int,
        title: str | None = None,
        color: str | None = None,
    ) -> LabelOperationResponse:
        """Update a Nextcloud Deck label

        Args:
            board_id: The ID of the board
            label_id: The ID of the label
            title: New title for the label
            color: New color for the label (hex format without #)
        """
        client = await get_client(ctx)
        await client.deck.update_label(board_id, label_id, title, color)
        return LabelOperationResponse(
            success=True,
            message="Label updated successfully",
            label_id=label_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Delete Deck Label",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_delete_label(
        ctx: Context, board_id: int, label_id: int
    ) -> LabelOperationResponse:
        """Delete a Nextcloud Deck label

        Args:
            board_id: The ID of the board
            label_id: The ID of the label
        """
        client = await get_client(ctx)
        await client.deck.delete_label(board_id, label_id)
        return LabelOperationResponse(
            success=True,
            message="Label deleted successfully",
            label_id=label_id,
            board_id=board_id,
        )

    # Card-Label Assignment Tools
    @mcp.tool(
        title="Assign Label to Deck Card",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @with_links
    @instrument_tool
    async def deck_assign_label_to_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int, label_id: int
    ) -> CardOperationResponse:
        """Assign a label to a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            label_id: The ID of the label to assign
        """
        client = await get_client(ctx)
        await client.deck.assign_label_to_card(board_id, stack_id, card_id, label_id)
        return CardOperationResponse(
            success=True,
            message="Label assigned to card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Remove Label from Deck Card",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @with_links
    @instrument_tool
    async def deck_remove_label_from_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int, label_id: int
    ) -> CardOperationResponse:
        """Remove a label from a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            label_id: The ID of the label to remove
        """
        client = await get_client(ctx)
        await client.deck.remove_label_from_card(board_id, stack_id, card_id, label_id)
        return CardOperationResponse(
            success=True,
            message="Label removed from card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    # Card-User Assignment Tools
    @mcp.tool(
        title="Assign User to Deck Card",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @with_links
    @instrument_tool
    async def deck_assign_user_to_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int, user_id: str
    ) -> CardOperationResponse:
        """Assign a user to a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            user_id: The user ID to assign
        """
        client = await get_client(ctx)
        await client.deck.assign_user_to_card(board_id, stack_id, card_id, user_id)
        return CardOperationResponse(
            success=True,
            message="User assigned to card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Unassign User from Deck Card",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("deck.write")
    @with_links
    @instrument_tool
    async def deck_unassign_user_from_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int, user_id: str
    ) -> CardOperationResponse:
        """Unassign a user from a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            user_id: The user ID to unassign
        """
        client = await get_client(ctx)
        await client.deck.unassign_user_from_card(board_id, stack_id, card_id, user_id)
        return CardOperationResponse(
            success=True,
            message="User unassigned from card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    # Card Dependency Tools
    @mcp.tool(
        title="Add Dependent Card to Deck Card",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @require_capability("deck", min_version="1.18.0")
    @with_links
    @instrument_tool
    async def deck_assign_dependent_card(
        ctx: Context,
        board_id: int,
        stack_id: int,
        card_id: int,
        dependent_card_id: int,
    ) -> CardOperationResponse:
        """Mark a Nextcloud Deck card as depending on another card.

        Mirrors Deck's "Add dependent card" action. The dependency is
        directional and stored on ``card_id``: it surfaces in that card's
        ``dependentCards`` list (visible via deck_get_card). You need read
        access to the dependent card.

        Args:
            board_id: The ID of the board containing the depending card
            stack_id: The ID of the stack containing the depending card
            card_id: The ID of the card that depends on another card
            dependent_card_id: The ID of the card that card_id depends on
        """
        client = await get_client(ctx)
        await client.deck.assign_dependent_card(
            board_id, stack_id, card_id, dependent_card_id
        )
        return CardOperationResponse(
            success=True,
            message=f"Card {dependent_card_id} added as a dependency of card {card_id}",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Remove Dependent Card from Deck Card",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("deck.write")
    @require_capability("deck", min_version="1.18.0")
    @with_links
    @instrument_tool
    async def deck_remove_dependent_card(
        ctx: Context,
        board_id: int,
        stack_id: int,
        card_id: int,
        dependent_card_id: int,
    ) -> CardOperationResponse:
        """Remove a dependency between two Nextcloud Deck cards.

        Removes the dependency of ``card_id`` on ``dependent_card_id`` that was
        created by deck_assign_dependent_card.

        Args:
            board_id: The ID of the board containing the depending card
            stack_id: The ID of the stack containing the depending card
            card_id: The ID of the card that depends on another card
            dependent_card_id: The ID of the dependency to remove
        """
        client = await get_client(ctx)
        await client.deck.remove_dependent_card(
            board_id, stack_id, card_id, dependent_card_id
        )
        return CardOperationResponse(
            success=True,
            message=f"Card {dependent_card_id} removed as a dependency of card {card_id}",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    # Card Comment Tools

    @mcp.tool(
        title="List Deck Card Comments",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_card_comments(
        ctx: Context,
        card_id: int,
        limit: int = 20,
        offset: int = 0,
        detail: DetailLevel = "summary",
        message_max_length: int | None = None,
        order: Literal["newest", "oldest"] = "newest",
    ) -> ListCardCommentsResponse:
        """List comments on a Nextcloud Deck card.

        Returns compact comments by default (dropping mentions, actor type and
        display name). Ordering and truncation apply within the returned page.

        Args:
            card_id: The ID of the card
            limit: Maximum number of comments to return (default 20, max 200)
            offset: Pagination offset (default 0)
            detail: "summary" (default) returns compact comments. "full"
                returns the complete comment objects.
            message_max_length: If set, truncate each comment message to this
                many characters.
            order: "newest" (default) or "oldest" — sort the page by creation
                time.
        """
        _validate_positive_length(message_max_length, "message_max_length")
        client = await get_client(ctx)
        try:
            comments = await client.deck.get_comments(
                card_id, limit=limit, offset=offset
            )
        except HTTPStatusError as e:
            raise _comment_http_error(e, operation="list", card_id=card_id) from e
        except RequestError as e:
            raise MCPError(
                code=-32603,
                message=(f"Network error listing comments on card {card_id}: {e}"),
            ) from e
        shaped = _shape_comments(
            comments,
            detail=detail,
            message_max_length=message_max_length,
            order=order,
        )
        return ListCardCommentsResponse(results=shaped, count=len(shaped))

    @mcp.tool(
        title="Create Deck Card Comment",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_create_card_comment(
        ctx: Context,
        card_id: int,
        message: str,
        parent_id: int | None = None,
        overflow: CommentOverflow = "error",
    ) -> CardCommentResponse:
        """Create a comment on a Nextcloud Deck card.

        Deck caps a comment at 1000 characters, measured after trimming
        whitespace and counted in Unicode code points. Markdown and @-mentions
        are NOT expanded before that check, so what you pass is what is counted.

        Check the length before calling and pick `overflow` accordingly:

        - 1000 characters or fewer: call as-is. `overflow` is ignored.
        - Longer, and the text is meant to be read on the card (activity
          updates, run summaries, changelogs): pass overflow="split" up front
          rather than guessing a shorter message. The text is cut at markdown
          heading, then paragraph, then line, then sentence, then word
          boundaries -- never mid-word -- each part is prefixed "(i/N)", and
          parts 2..N are posted as replies to part 1 so the card shows one
          thread. @-mentions are never split across parts. At most 10 parts.
          Past that, write the content to a note (nc_notes_create_note) or a
          file (nc_webdav_write_file), attach it with deck_attach_note /
          deck_attach_file, and post a short pointer comment instead.
        - Longer, and you would rather shorten it yourself: leave the default
          overflow="error". Nothing is posted and the error states the exact
          overage.

        Splitting is not atomic. If a later part fails, the earlier parts stay
        posted and the error names their comment ids -- resume from there
        instead of re-sending the whole message, which would duplicate them.

        Supports @-mentions: "@alice", or @"alice smith" for ids with spaces.

        Args:
            card_id: The ID of the card to comment on
            message: The comment text (max 1000 characters unless
                overflow="split")
            parent_id: Optional ID of a parent comment to reply to. When
                splitting, part 1 replies to this comment and parts 2..N reply
                to part 1.
            overflow: What to do when the message exceeds 1000 characters.
                "error" (the default) posts nothing and explains the overage.
                "split" posts the message as multiple threaded comments.

        Returns:
            CardCommentResponse. For a single comment, `comment` is it, `parts`
            is null and `part_count` is 1. When split, `comment` is part 1,
            `parts` lists every posted part in order (parts[0] is part 1), and
            `part_count` is how many were posted.
        """
        if overflow == "error" or measured_length(message) <= _COMMENT_MAX_LENGTH:
            _validate_comment_message(message)
            client = await get_client(ctx)
            try:
                comment = await client.deck.create_comment(
                    card_id, message, parent_id=parent_id
                )
            except HTTPStatusError as e:
                raise _comment_http_error(e, operation="create", card_id=card_id) from e
            except RequestError as e:
                raise MCPError(
                    code=-32603,
                    message=(
                        f"Network error creating a comment on card {card_id}: {e}"
                    ),
                ) from e
            return CardCommentResponse(comment=comment)

        client = await get_client(ctx)
        return await _post_split_comment(client, card_id, message, parent_id)

    @mcp.tool(
        title="Update Deck Card Comment",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_update_card_comment(
        ctx: Context, card_id: int, comment_id: int, message: str
    ) -> CardCommentResponse:
        """Update a Nextcloud Deck card comment.

        The same 1000-character limit applies as for creation (measured after
        trimming, in Unicode code points), but the message CANNOT be split: an
        update replaces one comment in place. To add more than fits, post a
        follow-up comment with deck_create_card_comment instead of growing an
        existing one past the limit.

        Only the comment's author can update it. The server returns 403
        otherwise.

        Args:
            card_id: The ID of the card the comment belongs to
            comment_id: The ID of the comment to update
            message: The new comment text (max 1000 characters)
        """
        _validate_comment_message(message)
        client = await get_client(ctx)
        try:
            comment = await client.deck.update_comment(card_id, comment_id, message)
        except HTTPStatusError as e:
            raise _comment_http_error(
                e,
                operation="update",
                card_id=card_id,
                comment_id=comment_id,
                message=message,
            ) from e
        except RequestError as e:
            raise MCPError(
                code=-32603,
                message=(
                    f"Network error updating comment {comment_id} on card "
                    f"{card_id}: {e}"
                ),
            ) from e
        return CardCommentResponse(comment=comment)

    @mcp.tool(
        title="Delete Deck Card Comment",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_delete_card_comment(
        ctx: Context, card_id: int, comment_id: int
    ) -> CardCommentOperationResponse:
        """Delete a Nextcloud Deck card comment

        Only the comment's author can delete it. The server returns 403 otherwise.

        Args:
            card_id: The ID of the card the comment belongs to
            comment_id: The ID of the comment to delete
        """
        client = await get_client(ctx)
        try:
            await client.deck.delete_comment(card_id, comment_id)
        except HTTPStatusError as e:
            raise _comment_http_error(
                e, operation="delete", card_id=card_id, comment_id=comment_id
            ) from e
        except RequestError as e:
            raise MCPError(
                code=-32603,
                message=(
                    f"Network error deleting comment {comment_id} on card "
                    f"{card_id}: {e}"
                ),
            ) from e
        return CardCommentOperationResponse(
            success=True,
            message="Comment deleted successfully",
            card_id=card_id,
            comment_id=comment_id,
        )

    @mcp.tool(
        title="Attach File to Deck Card",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write", "files.read")
    @instrument_tool
    async def deck_attach_file(
        ctx: Context, card_id: int, path: str
    ) -> AttachFileResponse:
        """Attach an existing Nextcloud file to a Deck card without copying.

        Creates a share of ``path`` with the card (``shareType=12``,
        ``shareWith=<card_id>``). The file stays in its original location.
        Clicking the attachment in the Deck UI opens the file in place.

        Generic over the user's Files: works for any file the caller can
        read — markdown notes, PDFs, images, spreadsheets, etc. Use
        :func:`deck_attach_note` if you have a Notes-app note ID and want
        the path resolved automatically. Calling twice with the same
        ``path`` creates two distinct shares — caller is responsible for
        de-duping.

        Args:
            card_id: The ID of the Deck card to attach to
            path: Path to the file in the user's Nextcloud Files (must start
                with "/", e.g. "/Documents/spec.pdf" or "/Notes/My Note.md")
        """
        if not path.startswith("/"):
            raise ToolError(
                f"path must start with '/', got: {path!r} "
                "(paths are relative to the user's Files root)"
            )
        client = await get_client(ctx)
        share = await client.sharing.create_share(
            path=path,
            share_with=str(card_id),
            share_type=_SHARE_TYPE_DECK,
            permissions=1,
        )
        return AttachFileResponse(
            attachment_id=int(share["id"]),
            card_id=card_id,
            path=path,
        )

    @mcp.tool(
        title="Attach Note to Deck Card",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("deck.write", "files.read", "notes.read")
    @instrument_tool
    async def deck_attach_note(
        ctx: Context, card_id: int, note_id: int
    ) -> AttachFileResponse:
        """Attach a Nextcloud Note to a Deck card without copying.

        Convenience wrapper: looks up the note's filesystem path from the
        Notes app settings + note metadata, then shares the file with the
        card (same mechanism as :func:`deck_attach_file`). The note remains
        editable in the Notes app. The card just shows a clickable link to
        it.

        Path is reconstructed as ``<notes_folder>/<category>/<title>.md``.
        If the note's title contains characters that the Notes app sanitises
        differently (rare), use :func:`deck_attach_file` with the explicit
        path instead.

        Args:
            card_id: The ID of the Deck card to attach to
            note_id: The ID of the Note to attach
        """
        client = await get_client(ctx)
        path = await _resolve_note_attach_path(client, note_id)
        share = await client.sharing.create_share(
            path=path,
            share_with=str(card_id),
            share_type=_SHARE_TYPE_DECK,
            permissions=1,
        )
        return AttachFileResponse(
            attachment_id=int(share["id"]),
            card_id=card_id,
            path=path,
        )

    @mcp.tool(
        title="List Deck Card Attachments",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_list_attachments(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> ListAttachmentsResponse:
        """List attachments on a Nextcloud Deck card.

        Returns both shared-file attachments (``type="file"``, created via
        :func:`deck_attach_file` / :func:`deck_attach_note`) and uploaded
        binary attachments (``type="deck_file"``).

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = await get_client(ctx)
        attachments = await client.deck.get_attachments(board_id, stack_id, card_id)
        return ListAttachmentsResponse(results=attachments, count=len(attachments))

    @mcp.tool(
        title="Delete Deck Card Attachment",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_delete_attachment(
        ctx: Context,
        board_id: int,
        stack_id: int,
        card_id: int,
        attachment_id: int,
    ) -> AttachmentOperationResponse:
        """Delete an attachment from a Nextcloud Deck card.

        For ``type="file"`` attachments this removes the share linking the
        file to the card. The underlying file in the user's Files is left
        untouched. For ``type="deck_file"`` blobs the binary is deleted from
        Deck's storage.

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            attachment_id: The ID of the attachment to delete
        """
        client = await get_client(ctx)
        await client.deck.delete_attachment(board_id, stack_id, card_id, attachment_id)
        return AttachmentOperationResponse(
            success=True,
            message="Attachment deleted successfully",
            card_id=card_id,
            attachment_id=attachment_id,
        )
