"""Deep links from tool responses back to the content in Nextcloud's web UI.

A tool opts in with :func:`with_links`. The decorator walks the response the tool
already built and fills in each linkable item's ``url`` field in place; nothing
else about the tool changes, and it still returns the same ``BaseResponse`` model.

The links live in the response body rather than in the result's ``_meta`` because
``_meta`` is dropped by every MCP client today, whereas MCPServer serialises the
model into both ``content`` and ``structuredContent``. See
``docs/ADR-035-tool-response-deep-links.md``.

A decorator rather than population at each construction site: ``DeckCard`` and
``DeckCardSummary`` carry ``stackId`` but no ``boardId``, so the board id has to
come from an ancestor model or from the tool's own arguments — and only a wrapper
sees both.
"""

import functools
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

# The resolver is app-agnostic despite the module it lives in: it validates that
# the configured Nextcloud base URL is something a browser can actually open.
from nextcloud_mcp_server.astrolabe_links import astrolabe_browser_base as browser_base
from nextcloud_mcp_server.models.deck import (
    CardOperationResponse,
    CreateCardResponse,
    DeckBoard,
    DeckCard,
    DeckCardSummary,
    DeckStack,
)
from nextcloud_mcp_server.models.notes import (
    AppendContentResponse,
    CreateNoteResponse,
    Note,
    NoteSearchResult,
    UpdateNoteResponse,
)
from nextcloud_mcp_server.models.webdav import FileInfo

logger = logging.getLogger(__name__)

# These routes spell out /index.php, exactly as ASTROLABE_APP_PATH does: they are
# URLs a browser opens, so BaseNextcloudClient._resolve_url — which prepends the
# prefix for bare /apps/... API calls — never sees them.
_NOTE_PATH = "/index.php/apps/notes/note/{note_id}"
_FILE_PATH = "/index.php/f/{file_id}"
_BOARD_PATH = "/index.php/apps/deck/board/{board_id}"
_CARD_PATH = "/index.php/apps/deck/board/{board_id}/card/{card_id}"


def _note_url(base: str, item: Any, ctx: dict[str, Any]) -> str | None:
    return base + _NOTE_PATH.format(note_id=item.id)


def file_url(base: str | None, file_id: int | str | None) -> str | None:
    """Link that opens a file in Nextcloud's Files UI, or None.

    Public because two callers reach it outside the response walk: the semantic
    search tool, whose file results carry the fileid as their ``doc_id``, and
    ``nc_webdav_read_file``, which resolves one per read. Both would otherwise
    re-spell ``_FILE_PATH``.

    Returns None when there is no browser-reachable base URL, and when the id is
    missing — PROPFIND does not always return one, and Nextcloud's /f/ route
    needs it, so an id-less link would 404. Absent beats broken: a caller cannot
    tell a dead link from a live one, whereas None is unambiguous.
    """
    if not base or file_id is None:
        return None
    return base + _FILE_PATH.format(file_id=file_id)


def _file_url(base: str, item: Any, ctx: dict[str, Any]) -> str | None:
    return file_url(base, item.file_id)


def _board_url(base: str, item: Any, ctx: dict[str, Any]) -> str | None:
    return base + _BOARD_PATH.format(board_id=item.id)


def _stack_url(base: str, item: Any, ctx: dict[str, Any]) -> str | None:
    # Deck has no per-stack route; its board is the closest thing that opens.
    return base + _BOARD_PATH.format(board_id=item.boardId)


def _card_url(base: str, item: Any, ctx: dict[str, Any]) -> str | None:
    board_id = ctx.get("board_id")
    if board_id is None:
        return None
    return base + _CARD_PATH.format(board_id=board_id, card_id=item.id)


def _card_operation_url(base: str, item: Any, ctx: dict[str, Any]) -> str | None:
    # This one carries both ids itself, so it never needs the walk context.
    return base + _CARD_PATH.format(board_id=item.board_id, card_id=item.card_id)


#: Item model -> the function that builds its browser URL. Every model listed
#: here must declare a ``url`` field; ``test_links.py`` asserts that, because a
#: registry entry whose model lacks the field would silently produce no link.
#:
#: DeckLabel is deliberately absent: every board and card carries a list of them
#: and Deck has no route that opens one.
_URL_BUILDERS: dict[
    type[BaseModel], Callable[[str, Any, dict[str, Any]], str | None]
] = {
    Note: _note_url,
    NoteSearchResult: _note_url,
    CreateNoteResponse: _note_url,
    UpdateNoteResponse: _note_url,
    AppendContentResponse: _note_url,
    FileInfo: _file_url,
    DeckBoard: _board_url,
    DeckStack: _stack_url,
    DeckCard: _card_url,
    DeckCardSummary: _card_url,
    CreateCardResponse: _card_url,
    CardOperationResponse: _card_operation_url,
}


def _board_context(item: BaseModel, ctx: dict[str, Any]) -> dict[str, Any]:
    """Overlay the board id ``item`` exposes, for use by its descendants.

    Cards know their stack but not their board, so the id comes from whichever
    ancestor has it — ``BoardOverviewResponse.board_id``, ``DeckStack.boardId``,
    or ``DeckBoard.id`` — falling back to the ``board_id`` the tool itself was
    called with. Returns ``ctx`` unchanged when this model adds nothing.
    """
    board_id = getattr(item, "board_id", None)
    if board_id is None:
        board_id = getattr(item, "boardId", None)
    if board_id is None and isinstance(item, DeckBoard):
        board_id = item.id
    if board_id is None:
        return ctx
    return {**ctx, "board_id": board_id}


def attach_urls(result: BaseModel, arguments: dict[str, Any] | None = None) -> None:
    """Fill in ``url`` on every linkable item inside ``result``, in place.

    ``arguments`` are the tool's own keyword arguments, which seed the walk
    context — that is where ``deck_get_cards(board_id=...)`` supplies the board id
    its cards lack. A no-op when no browser-reachable base URL is configured.
    """
    base = browser_base()
    if base is None:
        # browser_base already logged why, when the value was set but unusable.
        return
    _walk(result, base, dict(arguments or {}))


def _walk(item: BaseModel, base: str, ctx: dict[str, Any]) -> None:
    ctx = _board_context(item, ctx)
    fields = type(item).model_fields

    build = _URL_BUILDERS.get(type(item))
    if build is not None and "url" in fields:
        url = build(base, item, ctx)
        if url is not None:
            setattr(item, "url", url)

    # Response models are finite trees, so the recursion needs no depth cap or
    # cycle handling. Two levels deep is routine: BoardOverviewResponse holds
    # stacks, which hold cards.
    for name in fields:
        value = getattr(item, name, None)
        if isinstance(value, BaseModel):
            _walk(value, base, ctx)
        elif isinstance(value, (list, tuple)):
            for element in value:
                if isinstance(element, BaseModel):
                    _walk(element, base, ctx)


def with_links(fn):
    """Fill in ``url`` on the response this tool returns.

    Place it below ``@require_scopes`` and above ``@instrument_tool``::

        @mcp.tool(...)
        @require_scopes("notes.read")
        @with_links
        @instrument_tool
        async def nc_notes_search_notes(...) -> SearchNotesResponse: ...

    The wrapped tool returns the same model it built, so its declared return
    annotation stays truthful and the advertised ``outputSchema`` is unchanged.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await fn(*args, **kwargs)
        if isinstance(result, BaseModel):
            attach_urls(result, kwargs)
        return result

    # Marks the tool as opted in, so a test can check that every tool returning
    # a linkable model actually applies this decorator — the registry test alone
    # cannot see that. Written into __dict__ because that is precisely what
    # functools.wraps copies, so the flag stays visible through any outer
    # decorator (@require_scopes) on the fully decorated tool.
    wrapper.__dict__["__with_links__"] = True
    return wrapper
