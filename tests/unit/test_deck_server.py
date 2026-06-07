from datetime import datetime, timezone

import pytest

from nextcloud_mcp_server.models.deck import (
    DeckACL,
    DeckAssignedUser,
    DeckBoard,
    DeckCard,
    DeckCardSummary,
    DeckComment,
    DeckCommentSummary,
    DeckLabel,
    DeckPermissions,
    DeckStack,
    DeckUser,
)
from nextcloud_mcp_server.server.deck import (
    _SHARE_TYPE_DECK,
    _append_archived_cards,
    _apply_board_filters,
    _apply_stack_filters,
    _archived_cards_by_stack,
    _extract_uid,
    _filter_cards,
    _resolve_note_attach_path,
    _resolve_note_path,
    _shape_cards,
    _shape_comments,
    _summarize_card,
    _truncate_card_descriptions,
    _truncate_comment_message,
    _validate_positive_length,
)

pytestmark = pytest.mark.unit


# Fixtures ------------------------------------------------------------------


def _make_card(
    card_id: int,
    description: str | None = "desc",
    archived: bool = False,
    *,
    done: datetime | None = None,
    labels: list[DeckLabel] | None = None,
    assigned_users: list | None = None,
    attachment_count: int | None = None,
    comments_unread: int | None = None,
) -> DeckCard:
    return DeckCard(
        id=card_id,
        title=f"Card {card_id}",
        stackId=1,
        type="plain",
        order=card_id,
        archived=archived,
        owner="testuser",
        description=description,
        done=done,
        labels=labels,
        assignedUsers=assigned_users,
        attachmentCount=attachment_count,
        commentsUnread=comments_unread,
    )


def _make_comment(
    comment_id: int,
    message: str = "hello",
    *,
    actor: str = "alice",
    created: datetime | None = None,
) -> DeckComment:
    return DeckComment(
        id=comment_id,
        objectId=1,
        message=message,
        actorId=actor,
        actorType="users",
        actorDisplayName=actor.title(),
        creationDateTime=created or datetime(2024, 1, comment_id, tzinfo=timezone.utc),
        mentions=[],
    )


def _make_user(uid: str = "testuser") -> DeckUser:
    return DeckUser(primaryKey=uid, uid=uid, displayname=uid)


def _make_board(
    board_id: int = 1,
    *,
    labels: list[DeckLabel] | None = None,
    acl: list[DeckACL] | None = None,
    users: list[DeckUser] | None = None,
) -> DeckBoard:
    return DeckBoard(
        id=board_id,
        title=f"Board {board_id}",
        owner=_make_user(),
        color="FF0000",
        archived=False,
        labels=labels
        if labels is not None
        else [DeckLabel(id=1, title="L1", color="00FF00")],
        acl=acl if acl is not None else [],
        permissions=DeckPermissions(
            PERMISSION_READ=True,
            PERMISSION_EDIT=True,
            PERMISSION_MANAGE=True,
            PERMISSION_SHARE=True,
        ),
        users=users if users is not None else [_make_user("alice"), _make_user("bob")],
        deletedAt=0,
    )


def _make_stack(
    stack_id: int = 1,
    *,
    cards: list[DeckCard] | None = None,
) -> DeckStack:
    return DeckStack(
        id=stack_id,
        title=f"Stack {stack_id}",
        boardId=1,
        order=stack_id,
        deletedAt=0,
        cards=cards,
    )


# _truncate_card_descriptions ----------------------------------------------


def test_truncate_card_descriptions_no_op_when_limit_is_none():
    """When description_max_length is None, descriptions are left untouched."""
    cards = [_make_card(1, "x" * 5000)]
    _truncate_card_descriptions(cards, None)
    assert cards[0].description is not None
    assert len(cards[0].description) == 5000


def test_truncate_card_descriptions_truncates_long_descriptions():
    """Descriptions over the limit are truncated and marked with an ellipsis."""
    cards = [_make_card(1, "x" * 5000), _make_card(2, "short")]
    _truncate_card_descriptions(cards, 100)
    assert cards[0].description is not None
    assert len(cards[0].description) == 101  # 100 chars + ellipsis
    assert cards[0].description.endswith("…")
    assert cards[1].description == "short"


def test_truncate_card_descriptions_handles_none_description():
    """Cards with no description are skipped without error."""
    cards = [_make_card(1, None)]
    _truncate_card_descriptions(cards, 100)
    assert cards[0].description is None


def test_truncate_card_descriptions_at_exact_boundary():
    """Descriptions at exactly the limit should not be truncated."""
    cards = [_make_card(1, "x" * 100)]
    _truncate_card_descriptions(cards, 100)
    assert cards[0].description == "x" * 100


def test_truncate_card_descriptions_one_over_limit():
    """A description one character over the limit triggers truncation."""
    cards = [_make_card(1, "x" * 101)]
    _truncate_card_descriptions(cards, 100)
    assert cards[0].description is not None
    assert len(cards[0].description) == 101  # 100 chars + ellipsis
    assert cards[0].description.endswith("…")


def test_truncate_card_descriptions_shorter_than_limit_no_ellipsis():
    """A description shorter than the limit must not have an ellipsis appended."""
    cards = [_make_card(1, "hello")]
    _truncate_card_descriptions(cards, 1000)
    assert cards[0].description == "hello"


# _validate_positive_length ----------------------------------------


def test_validate_positive_length_accepts_none():
    """None is the documented sentinel for "no truncation"."""
    _validate_positive_length(None)


def test_validate_positive_length_accepts_positive():
    """Positive values pass through silently."""
    _validate_positive_length(1)
    _validate_positive_length(1000)


def test_validate_positive_length_rejects_zero():
    """Zero would wipe descriptions to a single ellipsis — reject at the boundary."""
    with pytest.raises(ValueError, match="must be positive"):
        _validate_positive_length(0)


def test_validate_positive_length_rejects_negative():
    """Negative values produce surprising slice semantics — reject at the boundary."""
    with pytest.raises(ValueError, match="must be positive"):
        _validate_positive_length(-10)


# _apply_board_filters ------------------------------------------------------


def test_apply_board_filters_defaults_preserve_fields():
    """With all include_* flags True, no fields are cleared."""
    board = _make_board()
    result = _apply_board_filters(
        board, include_acl=True, include_users=True, include_labels=True
    )
    assert len(result.labels) == 1
    assert len(result.users) == 2


def test_apply_board_filters_excludes_acl():
    """include_acl=False clears the acl list."""
    board = _make_board(
        acl=[
            DeckACL(
                id=1,
                participant=_make_user("alice"),
                type=0,
                boardId=1,
                permissionEdit=True,
                permissionShare=True,
                permissionManage=False,
                owner=False,
            )
        ]
    )
    result = _apply_board_filters(
        board, include_acl=False, include_users=True, include_labels=True
    )
    assert result.acl == []


def test_apply_board_filters_excludes_users():
    """include_users=False clears the users list."""
    board = _make_board()
    result = _apply_board_filters(
        board, include_acl=True, include_users=False, include_labels=True
    )
    assert result.users == []


def test_apply_board_filters_excludes_labels():
    """include_labels=False clears the labels list."""
    board = _make_board()
    result = _apply_board_filters(
        board, include_acl=True, include_users=True, include_labels=False
    )
    assert result.labels == []


def test_apply_board_filters_excludes_all():
    """All include_* flags False clears every filterable list."""
    board = _make_board()
    result = _apply_board_filters(
        board, include_acl=False, include_users=False, include_labels=False
    )
    assert result.acl == []
    assert result.users == []
    assert result.labels == []


# Shared default kwargs for _apply_stack_filters in summary mode ------------

_STACK_DEFAULTS = dict(
    detail="summary",
    status="open",
    label=None,
    assigned_to=None,
    description_max_length=None,
    description_preview_length=140,
)


# _filter_cards -------------------------------------------------------------


def test_filter_cards_open_excludes_archived_and_done():
    """status="open" drops both archived and explicitly-done cards."""
    done_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    cards = [
        _make_card(1),
        _make_card(2, archived=True),
        _make_card(3, done=done_at),
    ]
    result = _filter_cards(cards, status="open", label=None, assigned_to=None)
    assert [c.id for c in result] == [1]


def test_filter_cards_done_keeps_only_done_and_not_archived():
    """status="done" keeps done cards that are not archived."""
    done_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    cards = [_make_card(1), _make_card(2, done=done_at)]
    result = _filter_cards(cards, status="done", label=None, assigned_to=None)
    assert [c.id for c in result] == [2]


def test_filter_cards_archived_keeps_only_archived():
    """status="archived" keeps only archived cards."""
    cards = [_make_card(1), _make_card(2, archived=True)]
    result = _filter_cards(cards, status="archived", label=None, assigned_to=None)
    assert [c.id for c in result] == [2]


def test_filter_cards_statuses_partition_the_board():
    """open/done/archived are non-overlapping; a done+archived card counts
    only as "archived", not "done"."""
    done_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    open_card = _make_card(1)
    done_card = _make_card(2, done=done_at)
    done_and_archived = _make_card(3, done=done_at, archived=True)
    cards = [open_card, done_card, done_and_archived]

    assert [
        c.id for c in _filter_cards(cards, status="open", label=None, assigned_to=None)
    ] == [1]
    assert [
        c.id for c in _filter_cards(cards, status="done", label=None, assigned_to=None)
    ] == [2]
    assert [
        c.id
        for c in _filter_cards(cards, status="archived", label=None, assigned_to=None)
    ] == [3]


def test_filter_cards_all_keeps_everything():
    """status="all" applies no status filter."""
    cards = [_make_card(1), _make_card(2, archived=True)]
    result = _filter_cards(cards, status="all", label=None, assigned_to=None)
    assert [c.id for c in result] == [1, 2]


def test_filter_cards_by_label_matches_title_exactly():
    """label filtering matches the exact label title."""
    a = _make_card(1, labels=[DeckLabel(id=1, title="bug", color="f00")])
    b = _make_card(2, labels=[DeckLabel(id=2, title="feature", color="0f0")])
    result = _filter_cards([a, b], status="all", label="bug", assigned_to=None)
    assert [c.id for c in result] == [1]


def test_filter_cards_by_assignee_handles_both_user_shapes():
    """assigned_to matches DeckUser and DeckAssignedUser shapes alike."""
    direct = _make_card(1, assigned_users=[_make_user("alice")])
    wrapped = _make_card(
        2,
        assigned_users=[
            DeckAssignedUser(id=9, participant=_make_user("bob"), cardId=2, type=0)
        ],
    )
    result = _filter_cards(
        [direct, wrapped], status="all", label=None, assigned_to="bob"
    )
    assert [c.id for c in result] == [2]


# _extract_uid --------------------------------------------------------------


def test_extract_uid_from_deck_user():
    assert _extract_uid(_make_user("alice")) == "alice"


def test_extract_uid_from_assigned_user():
    assigned = DeckAssignedUser(id=1, participant=_make_user("bob"), cardId=1, type=0)
    assert _extract_uid(assigned) == "bob"


# _summarize_card -----------------------------------------------------------


def test_summarize_card_projects_compact_fields():
    """Summary carries flat label titles, assignee uids, counts, and a preview."""
    card = _make_card(
        1,
        description="x" * 50,
        labels=[DeckLabel(id=1, title="bug", color="f00")],
        assigned_users=[_make_user("alice")],
        attachment_count=3,
        comments_unread=2,
    )
    summary = _summarize_card(card, description_preview_length=10)
    assert isinstance(summary, DeckCardSummary)
    assert summary.labels == ["bug"]
    assert summary.assignedUsers == ["alice"]
    assert summary.attachmentCount == 3
    assert summary.commentsUnread == 2
    assert summary.hasDescription is True
    assert summary.descriptionPreview is not None
    assert summary.descriptionPreview.endswith("…")
    assert len(summary.descriptionPreview) == 11  # 10 chars + ellipsis


def test_summarize_card_short_description_has_no_ellipsis():
    """A description within the preview length is carried verbatim."""
    summary = _summarize_card(_make_card(1, description="hi"), 140)
    assert summary.descriptionPreview == "hi"
    assert summary.hasDescription is True


def test_summarize_card_empty_description():
    """An empty/whitespace description yields hasDescription=False, no preview."""
    summary = _summarize_card(_make_card(1, description="   "), 140)
    assert summary.hasDescription is False
    assert summary.descriptionPreview is None


# _shape_cards --------------------------------------------------------------


def test_shape_cards_summary_returns_summaries():
    """detail="summary" projects every (filtered) card to a DeckCardSummary."""
    cards = [_make_card(1), _make_card(2, archived=True)]
    result = _shape_cards(
        cards,
        detail="summary",
        status="open",
        label=None,
        assigned_to=None,
        description_max_length=None,
        description_preview_length=140,
    )
    assert [type(c) for c in result] == [DeckCardSummary]
    assert result[0].id == 1


def test_shape_cards_full_returns_truncated_full_cards():
    """detail="full" returns DeckCard objects with descriptions truncated."""
    cards = [_make_card(1, description="x" * 50)]
    result = _shape_cards(
        cards,
        detail="full",
        status="all",
        label=None,
        assigned_to=None,
        description_max_length=10,
        description_preview_length=140,
    )
    assert isinstance(result[0], DeckCard)
    assert result[0].description is not None
    assert result[0].description.endswith("…")


# _apply_stack_filters ------------------------------------------------------


def test_apply_stack_filters_include_cards_false_strips_cards():
    """include_cards=False sets cards to None regardless of other flags."""
    stack = _make_stack(cards=[_make_card(1), _make_card(2, archived=True)])
    result = _apply_stack_filters(stack, include_cards=False, **_STACK_DEFAULTS)
    assert result.cards is None


def test_apply_stack_filters_summary_excludes_archived_by_default():
    """Default status="open" filters out archived cards and returns summaries."""
    stack = _make_stack(
        cards=[_make_card(1, archived=False), _make_card(2, archived=True)]
    )
    result = _apply_stack_filters(stack, include_cards=True, **_STACK_DEFAULTS)
    assert result.cards is not None
    assert [c.id for c in result.cards] == [1]
    assert all(isinstance(c, DeckCardSummary) for c in result.cards)


def test_apply_stack_filters_status_all_keeps_archived():
    """status="all" retains archived cards."""
    stack = _make_stack(
        cards=[_make_card(1, archived=False), _make_card(2, archived=True)]
    )
    kwargs = {**_STACK_DEFAULTS, "status": "all"}
    result = _apply_stack_filters(stack, include_cards=True, **kwargs)
    assert result.cards is not None
    assert [c.id for c in result.cards] == [1, 2]


def test_apply_stack_filters_full_truncates_descriptions_after_filter():
    """detail="full" truncation runs on the post-status-filter card set."""
    stack = _make_stack(
        cards=[
            _make_card(1, description="x" * 50, archived=False),
            _make_card(2, description="y" * 50, archived=True),
        ]
    )
    kwargs = {**_STACK_DEFAULTS, "detail": "full", "description_max_length": 10}
    result = _apply_stack_filters(stack, include_cards=True, **kwargs)
    assert result.cards is not None
    assert len(result.cards) == 1
    assert isinstance(result.cards[0], DeckCard)
    assert result.cards[0].description is not None
    assert result.cards[0].description.endswith("…")


def test_apply_stack_filters_handles_none_cards():
    """A stack with no cards (cards=None) is left untouched."""
    stack = _make_stack(cards=None)
    result = _apply_stack_filters(stack, include_cards=True, **_STACK_DEFAULTS)
    assert result.cards is None


def test_apply_stack_filters_all_filtered_yields_empty_list_not_none():
    """A stack whose cards are all filtered out yields cards == [], not None.

    Pin the contract: include_cards=True with all cards filtered out
    means "the stack was loaded but had nothing to show", which is
    semantically distinct from include_cards=False (cards=None,
    "explicitly suppressed").
    """
    stack = _make_stack(
        cards=[_make_card(1, archived=True), _make_card(2, archived=True)]
    )
    result = _apply_stack_filters(stack, include_cards=True, **_STACK_DEFAULTS)
    assert result.cards == []
    assert result.cards is not None


# _shape_comments -----------------------------------------------------------


def test_shape_comments_summary_drops_actor_metadata():
    """detail="summary" projects comments to compact DeckCommentSummary rows."""
    comments = [_make_comment(1, "hi", actor="alice")]
    result = _shape_comments(
        comments, detail="summary", message_max_length=None, order="newest"
    )
    assert isinstance(result[0], DeckCommentSummary)
    assert result[0].actorId == "alice"
    assert result[0].message == "hi"


def test_shape_comments_newest_first():
    """order="newest" sorts the page by creation time descending."""
    comments = [_make_comment(1), _make_comment(3), _make_comment(2)]
    result = _shape_comments(
        comments, detail="summary", message_max_length=None, order="newest"
    )
    assert [c.id for c in result] == [3, 2, 1]


def test_shape_comments_oldest_first():
    """order="oldest" sorts the page by creation time ascending."""
    comments = [_make_comment(3), _make_comment(1), _make_comment(2)]
    result = _shape_comments(
        comments, detail="summary", message_max_length=None, order="oldest"
    )
    assert [c.id for c in result] == [1, 2, 3]


def test_shape_comments_full_truncates_message():
    """detail="full" keeps DeckComment but truncates long messages when asked."""
    comments = [_make_comment(1, "x" * 50)]
    result = _shape_comments(
        comments, detail="full", message_max_length=10, order="newest"
    )
    assert isinstance(result[0], DeckComment)
    assert result[0].message.endswith("…")
    assert len(result[0].message) == 11


def test_truncate_comment_message_no_op_when_within_limit():
    assert _truncate_comment_message("short", 100) == "short"
    assert _truncate_comment_message("short", None) == "short"


# _resolve_note_path -------------------------------------------------------


def test_resolve_note_path_no_category():
    """Path is /<notes_folder>/<title>.md when no category."""
    assert _resolve_note_path("Notes", "", "My Note") == "/Notes/My Note.md"


def test_resolve_note_path_with_category():
    """Category is inserted as a sub-path."""
    assert _resolve_note_path("Notes", "Work", "Standup") == "/Notes/Work/Standup.md"


def test_resolve_note_path_with_nested_category():
    """Nested categories (Notes app supports `/`-separated) are preserved."""
    assert _resolve_note_path("Notes", "Work/Q4", "Plan") == "/Notes/Work/Q4/Plan.md"


def test_resolve_note_path_strips_redundant_slashes():
    """Leading/trailing slashes on inputs do not produce `//` in the result."""
    assert _resolve_note_path("/Notes/", "/Work/", "Title") == "/Notes/Work/Title.md"


def test_resolve_note_path_custom_notes_folder():
    """Honors a non-default notes_folder from Notes app settings."""
    assert (
        _resolve_note_path("Documents/Notes", "", "Idea") == "/Documents/Notes/Idea.md"
    )


# Share-type constant -------------------------------------------------------


def test_share_type_deck_constant_matches_deck_app():
    """Deck UI uses shareType=12 (IShare::TYPE_DECK) — must not drift."""
    assert _SHARE_TYPE_DECK == 12


# _resolve_note_attach_path (camelCase notesPath guard) ---------------------


async def test_resolve_note_attach_path_honors_camelcase_notes_path(mocker):
    """Custom notes folders configured in the Notes app must be honored.

    Regression: the Notes API returns the folder under ``notesPath`` (camelCase,
    see ``models/notes.py:43``). An earlier draft of this code looked up
    ``notes_path`` (snake_case) and silently fell back to the default ``"Notes"``,
    producing 404s for users with a non-default folder. This test pins the
    correct key so that bug can't reappear.
    """
    client = mocker.AsyncMock()
    client.notes.get_settings.return_value = {"notesPath": "Documents/MyNotes"}
    client.notes.get_note.return_value = {
        "id": 42,
        "title": "Q4 Plan",
        "category": "Work",
    }

    path = await _resolve_note_attach_path(client, note_id=42)

    assert path == "/Documents/MyNotes/Work/Q4 Plan.md"
    client.notes.get_settings.assert_awaited_once()
    client.notes.get_note.assert_awaited_once_with(42)


async def test_resolve_note_attach_path_falls_back_to_default_when_setting_missing(
    mocker,
):
    """Missing/empty ``notesPath`` falls back to the documented default."""
    client = mocker.AsyncMock()
    client.notes.get_settings.return_value = {}
    client.notes.get_note.return_value = {
        "id": 1,
        "title": "Idea",
        "category": "",
    }

    path = await _resolve_note_attach_path(client, note_id=1)

    assert path == "/Notes/Idea.md"


async def test_resolve_note_attach_path_handles_null_category(mocker):
    """A note with ``category=None`` (rather than ``""``) must not crash."""
    client = mocker.AsyncMock()
    client.notes.get_settings.return_value = {"notesPath": "Notes"}
    client.notes.get_note.return_value = {
        "id": 7,
        "title": "Bare",
        "category": None,
    }

    path = await _resolve_note_attach_path(client, note_id=7)

    assert path == "/Notes/Bare.md"


# Archived-card merge (issue #842) -----------------------------------------


def test_append_archived_cards_merges_onto_existing():
    """Archived cards are appended after the stack's existing (open) cards."""
    stack = _make_stack(cards=[_make_card(1, archived=False)])
    _append_archived_cards(stack, [_make_card(2, archived=True)])
    assert stack.cards is not None
    assert [c.id for c in stack.cards] == [1, 2]


def test_append_archived_cards_onto_none_cards():
    """A stack with cards=None gets a fresh list of the archived cards."""
    stack = _make_stack(cards=None)
    _append_archived_cards(stack, [_make_card(2, archived=True)])
    assert stack.cards is not None
    assert [c.id for c in stack.cards] == [2]


async def test_archived_cards_by_stack_maps_stack_id_to_cards(mocker):
    """The helper keys archived cards by stack id, coercing None to []."""
    archived = [
        _make_stack(stack_id=3, cards=[_make_card(10, archived=True)]),
        _make_stack(stack_id=4, cards=None),
    ]
    client = mocker.MagicMock()
    client.deck.get_archived_stacks = mocker.AsyncMock(return_value=archived)

    result = await _archived_cards_by_stack(client, board_id=1)

    assert set(result) == {3, 4}
    assert [c.id for c in result[3]] == [10]
    assert result[4] == []
    client.deck.get_archived_stacks.assert_awaited_once_with(1)
