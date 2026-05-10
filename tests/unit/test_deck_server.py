import pytest

from nextcloud_mcp_server.models.deck import (
    DeckACL,
    DeckBoard,
    DeckCard,
    DeckLabel,
    DeckPermissions,
    DeckStack,
    DeckUser,
)
from nextcloud_mcp_server.server.deck import (
    _SHARE_TYPE_DECK,
    _apply_board_filters,
    _apply_card_filters,
    _apply_stack_filters,
    _resolve_note_attach_path,
    _resolve_note_path,
    _truncate_card_descriptions,
    _validate_description_max_length,
)

pytestmark = pytest.mark.unit


# Fixtures ------------------------------------------------------------------


def _make_card(
    card_id: int,
    description: str | None = "desc",
    archived: bool = False,
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


# _validate_description_max_length ----------------------------------------


def test_validate_description_max_length_accepts_none():
    """None is the documented sentinel for "no truncation"."""
    _validate_description_max_length(None)


def test_validate_description_max_length_accepts_positive():
    """Positive values pass through silently."""
    _validate_description_max_length(1)
    _validate_description_max_length(1000)


def test_validate_description_max_length_rejects_zero():
    """Zero would wipe descriptions to a single ellipsis — reject at the boundary."""
    with pytest.raises(ValueError, match="must be positive"):
        _validate_description_max_length(0)


def test_validate_description_max_length_rejects_negative():
    """Negative values produce surprising slice semantics — reject at the boundary."""
    with pytest.raises(ValueError, match="must be positive"):
        _validate_description_max_length(-10)


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


# _apply_stack_filters ------------------------------------------------------


def test_apply_stack_filters_include_cards_false_strips_cards():
    """include_cards=False sets cards to None regardless of other flags."""
    stack = _make_stack(cards=[_make_card(1), _make_card(2, archived=True)])
    result = _apply_stack_filters(
        stack,
        include_cards=False,
        include_archived_cards=True,
        description_max_length=None,
    )
    assert result.cards is None


def test_apply_stack_filters_excludes_archived_by_default():
    """include_archived_cards=False filters out archived cards."""
    stack = _make_stack(
        cards=[_make_card(1, archived=False), _make_card(2, archived=True)]
    )
    result = _apply_stack_filters(
        stack,
        include_cards=True,
        include_archived_cards=False,
        description_max_length=None,
    )
    assert result.cards is not None
    assert [c.id for c in result.cards] == [1]


def test_apply_stack_filters_keeps_archived_when_requested():
    """include_archived_cards=True retains archived cards."""
    stack = _make_stack(
        cards=[_make_card(1, archived=False), _make_card(2, archived=True)]
    )
    result = _apply_stack_filters(
        stack,
        include_cards=True,
        include_archived_cards=True,
        description_max_length=None,
    )
    assert result.cards is not None
    assert [c.id for c in result.cards] == [1, 2]


def test_apply_stack_filters_truncates_descriptions_after_archive_filter():
    """Truncation runs on the post-archive-filter card set."""
    stack = _make_stack(
        cards=[
            _make_card(1, description="x" * 50, archived=False),
            _make_card(2, description="y" * 50, archived=True),
        ]
    )
    result = _apply_stack_filters(
        stack,
        include_cards=True,
        include_archived_cards=False,
        description_max_length=10,
    )
    assert result.cards is not None
    assert len(result.cards) == 1
    assert result.cards[0].description is not None
    assert result.cards[0].description.endswith("…")


def test_apply_stack_filters_handles_none_cards():
    """A stack with no cards (cards=None) is left untouched."""
    stack = _make_stack(cards=None)
    result = _apply_stack_filters(
        stack,
        include_cards=True,
        include_archived_cards=False,
        description_max_length=10,
    )
    assert result.cards is None


def test_apply_stack_filters_all_archived_yields_empty_list_not_none():
    """A stack whose cards are all archived yields cards == [], not None.

    Pin the contract: include_cards=True with all cards filtered out
    means "the stack was loaded but had nothing to show", which is
    semantically distinct from include_cards=False (cards=None,
    "explicitly suppressed"). Callers checking ``stack.cards is None``
    can use that to distinguish the two states.
    """
    stack = _make_stack(
        cards=[_make_card(1, archived=True), _make_card(2, archived=True)]
    )
    result = _apply_stack_filters(
        stack,
        include_cards=True,
        include_archived_cards=False,
        description_max_length=None,
    )
    assert result.cards == []
    assert result.cards is not None


# _apply_card_filters -------------------------------------------------------


def test_apply_card_filters_excludes_archived_by_default():
    """include_archived_cards=False filters archived cards out of the flat list."""
    cards = [
        _make_card(1, archived=False),
        _make_card(2, archived=True),
        _make_card(3, archived=False),
    ]
    result = _apply_card_filters(
        cards, include_archived_cards=False, description_max_length=None
    )
    assert [c.id for c in result] == [1, 3]


def test_apply_card_filters_keeps_archived_when_requested():
    """include_archived_cards=True retains archived cards."""
    cards = [_make_card(1, archived=False), _make_card(2, archived=True)]
    result = _apply_card_filters(
        cards, include_archived_cards=True, description_max_length=None
    )
    assert [c.id for c in result] == [1, 2]


def test_apply_card_filters_truncates_descriptions():
    """description_max_length is honored on the returned cards."""
    cards = [_make_card(1, description="x" * 50)]
    result = _apply_card_filters(
        cards, include_archived_cards=True, description_max_length=10
    )
    assert result[0].description is not None
    assert result[0].description.endswith("…")


def test_apply_card_filters_empty_list_is_noop():
    """An empty input returns an empty output."""
    result = _apply_card_filters(
        [], include_archived_cards=False, description_max_length=10
    )
    assert result == []


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
