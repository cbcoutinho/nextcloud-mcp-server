"""Unit tests for the deep links that point tool responses back at Nextcloud.

The contract these pin: a link is either usable or absent. A URL built from a
missing id would 404, which is worse than no link at all — the caller cannot tell
a broken link from a working one, whereas ``None`` is unambiguous.
"""

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from nextcloud_mcp_server.links import (
    _URL_BUILDERS,
    attach_urls,
    file_url,
    with_links,
)
from nextcloud_mcp_server.models.deck import (
    BoardOverviewResponse,
    CardOperationResponse,
    CreateCardResponse,
    DeckCard,
    DeckCardSummary,
    DeckStack,
    ListCardsResponse,
    StackOverview,
)
from nextcloud_mcp_server.models.notes import (
    CreateNoteResponse,
    Note,
    NoteSearchResult,
    SearchNotesResponse,
)
from nextcloud_mcp_server.models.webdav import (
    DirectoryListing,
    FileInfo,
    FilesByTagResponse,
)

pytestmark = pytest.mark.unit

BASE = "https://nc.example.com"


def _settings(browser_url: str | None = BASE) -> SimpleNamespace:
    """Settings-shaped object exposing only what ``browser_base`` reads."""
    return SimpleNamespace(nextcloud_browser_url=browser_url)


def _attach(result, arguments=None, browser_url: str | None = BASE):
    """Run the walker with a stubbed browser base URL."""
    with patch(
        "nextcloud_mcp_server.astrolabe_links.get_settings",
        return_value=_settings(browser_url),
    ):
        attach_urls(result, arguments)
    return result


def _note(note_id: int = 42) -> Note:
    return Note(id=note_id, title="T", content="c", category="", modified=0, etag="e")


def _card(card_id: int = 42, stack_id: int = 3) -> DeckCard:
    return DeckCard(
        id=card_id,
        title="C",
        stackId=stack_id,
        type="plain",
        order=0,
        archived=False,
        owner="alice",
    )


# --- route builders ---------------------------------------------------------


def test_note_url_points_at_the_notes_app():
    assert _attach(_note(42)).url == f"{BASE}/index.php/apps/notes/note/42"


def test_note_write_envelopes_are_linked_too():
    """An agent that just created a note should get a link to open it."""
    created = _attach(CreateNoteResponse(id=7, title="T", category="", etag="e"))
    assert created.url == f"{BASE}/index.php/apps/notes/note/7"


def test_file_url_uses_the_fileid_permalink():
    info = FileInfo(name="a.txt", path="/a.txt", is_directory=False, file_id=99)
    assert _attach(info).url == f"{BASE}/index.php/f/99"


def test_file_without_a_fileid_gets_no_link():
    """PROPFIND does not always return one, and /f/ needs it."""
    info = FileInfo(name="a.txt", path="/a.txt", is_directory=False, file_id=None)
    assert _attach(info).url is None


def test_file_url_builder_is_callable_outside_the_walk():
    """Two callers reach it directly: the semantic search tool (whose file
    results carry the fileid as their doc_id) and nc_webdav_read_file."""
    assert file_url(BASE, 99) == f"{BASE}/index.php/f/99"
    # doc_id arrives as a str on the search path.
    assert file_url(BASE, "99") == f"{BASE}/index.php/f/99"


@pytest.mark.parametrize(
    "base, file_id", [(None, 99), ("", 99), (BASE, None)], ids=["none", "empty", "noid"]
)
def test_file_url_builder_omits_the_link_when_it_would_be_broken(base, file_id):
    assert file_url(base, file_id) is None


def test_stack_links_to_its_board():
    """Deck has no route that opens a single stack."""
    stack = DeckStack(id=3, title="S", boardId=7, order=0, deletedAt=0)
    assert _attach(stack).url == f"{BASE}/index.php/apps/deck/board/7"


def test_card_operation_response_uses_the_ids_it_carries():
    result = _attach(CardOperationResponse(card_id=42, stack_id=3, board_id=7))
    assert result.url == f"{BASE}/index.php/apps/deck/board/7/card/42"


# --- board-id threading -----------------------------------------------------


def test_card_takes_the_board_id_from_the_tools_arguments():
    """DeckCard carries stackId but no boardId — this is why a decorator exists."""
    response = ListCardsResponse(cards=[_card(42)], total=1)
    _attach(response, {"board_id": 7, "stack_id": 3})
    assert response.cards[0].url == f"{BASE}/index.php/apps/deck/board/7/card/42"


def test_card_takes_the_board_id_from_an_enclosing_model():
    """deck_get_board_overview does not take a board_id kwarg per card."""
    response = BoardOverviewResponse(
        board_id=7,
        title="B",
        total_cards=1,
        stacks=[
            StackOverview(
                id=3,
                title="S",
                card_count=1,
                cards=[DeckCardSummary(id=42, title="C", stackId=3)],
            )
        ],
    )
    _attach(response, {})
    assert (
        response.stacks[0].cards[0].url == f"{BASE}/index.php/apps/deck/board/7/card/42"
    )


def test_card_with_no_known_board_gets_no_link():
    response = ListCardsResponse(cards=[_card(42)], total=1)
    _attach(response, {})
    assert response.cards[0].url is None


def test_created_card_is_linked_from_the_tools_board_id():
    created = CreateCardResponse(id=42, title="C", stackId=3)
    _attach(created, {"board_id": 7, "stack_id": 3, "title": "C"})
    assert created.url == f"{BASE}/index.php/apps/deck/board/7/card/42"


# --- nested collections -----------------------------------------------------


def test_every_item_in_a_list_response_is_linked():
    response = SearchNotesResponse(
        results=[NoteSearchResult(id=1, title="a"), NoteSearchResult(id=2, title="b")],
        query="q",
        total_found=2,
    )
    _attach(response)
    assert [r.url for r in response.results] == [
        f"{BASE}/index.php/apps/notes/note/1",
        f"{BASE}/index.php/apps/notes/note/2",
    ]


def test_directory_listing_links_each_entry_that_can_be_linked():
    listing = DirectoryListing(
        path="/",
        files=[
            FileInfo(name="a", path="/a", is_directory=False, file_id=1),
            FileInfo(name="b", path="/b", is_directory=False, file_id=None),
        ],
        total_count=2,
        directories_count=0,
        files_count=2,
    )
    _attach(listing)
    assert listing.files[0].url == f"{BASE}/index.php/f/1"
    assert listing.files[1].url is None


def test_files_by_tag_links_each_entry_that_can_be_linked():
    """nc_webdav_find_by_tag_name's results link the same way a directory listing's do -
    both hold plain FileInfo entries, so one registry entry covers both tools."""
    found = FilesByTagResponse(
        tag="invoice",
        tag_id=7,
        files=[
            FileInfo(name="a", path="/a", is_directory=False, file_id=1),
            FileInfo(name="b", path="/b", is_directory=False, file_id=None),
        ],
        total_count=2,
    )
    _attach(found)
    assert found.files[0].url == f"{BASE}/index.php/f/1"
    assert found.files[1].url is None


# --- absent or unusable configuration ---------------------------------------


def test_no_configured_base_url_means_no_links():
    """Rather than a relative or half-formed URL."""
    assert _attach(_note(), browser_url=None).url is None


def test_unusable_base_url_means_no_links():
    """A bare host:port is not something a browser can open."""
    assert _attach(_note(), browser_url="internal-host:8080").url is None


def test_built_urls_are_absolute_and_browser_openable():
    parsed = urlparse(_attach(_note()).url)
    assert parsed.scheme in ("http", "https")
    assert parsed.netloc


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    assert _attach(_note(42), browser_url=f"{BASE}/").url == (
        f"{BASE}/index.php/apps/notes/note/42"
    )


# --- registry integrity -----------------------------------------------------


def test_every_registered_model_declares_a_url_field():
    """The walker silently skips a registered model that lacks the field.

    This is the guard that keeps the registry and the models in step: adding a
    builder without the matching field would otherwise produce no link and no
    error.
    """
    missing = [
        model.__name__ for model in _URL_BUILDERS if "url" not in model.model_fields
    ]
    assert not missing, f"registered without a url field: {missing}"


# --- the decorator ----------------------------------------------------------


async def test_decorator_returns_the_same_model_instance():
    """It fills a field in place; it must not re-wrap or copy the response."""

    @with_links
    async def tool(**kwargs):
        return _note(42)

    with patch(
        "nextcloud_mcp_server.astrolabe_links.get_settings",
        return_value=_settings(),
    ):
        result = await tool(board_id=7)

    assert isinstance(result, Note)
    assert result.url == f"{BASE}/index.php/apps/notes/note/42"


async def test_decorator_passes_through_a_non_model_return():
    @with_links
    async def tool():
        return {"raw": "dict"}

    assert await tool() == {"raw": "dict"}
