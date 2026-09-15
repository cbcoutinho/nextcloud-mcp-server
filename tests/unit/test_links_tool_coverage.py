"""Every tool that *could* return a deep link actually applies `@with_links`.

`test_links.py` checks the other half — that every model in the registry
declares a `url` field. Neither check implies the other, and this is the gap
that let eight of the nine mutating Deck card tools silently return
`CardOperationResponse.url = None` in the first cut of this feature, in direct
contradiction of that field's own description.

The failure mode is invisible at runtime: the field is optional, so a forgotten
decorator produces a `null` rather than an error. Only a test that walks the
tool registry can see it.
"""

import inspect
import typing

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from nextcloud_mcp_server.links import _URL_BUILDERS
from nextcloud_mcp_server.server.deck import configure_deck_tools
from nextcloud_mcp_server.server.notes import configure_notes_tools
from nextcloud_mcp_server.server.webdav import configure_webdav_tools

pytestmark = pytest.mark.unit

#: Tools that return a linkable model but must NOT carry a link, with the reason.
#: Anything added here needs a matching comment at the tool itself.
EXEMPT: dict[str, str] = {
    "deck_delete_card": "the card no longer exists, so a link would 404",
}


def _linkable_models(annotation: object, seen: set[type] | None = None) -> set[type]:
    """Registered models reachable from a return annotation.

    Walks nested models and their generic arguments, so a response that only
    holds linkable items in a list (``SearchNotesResponse.results``) counts just
    as much as one that is linkable itself (``Note``).
    """
    seen = seen if seen is not None else set()
    found: set[type] = set()

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return found
        seen.add(annotation)
        if annotation in _URL_BUILDERS:
            found.add(annotation)
        for field in annotation.model_fields.values():
            found |= _linkable_models(field.annotation, seen)
        return found

    # list[X], X | None, dict[str, X] ... — recurse through the arguments.
    for arg in typing.get_args(annotation):
        found |= _linkable_models(arg, seen)
    return found


def _all_tools() -> dict:
    """Register the three link-carrying tool modules and return them by name."""
    mcp = MCPServer(name="test-link-coverage")
    configure_deck_tools(mcp)
    configure_notes_tools(mcp)
    configure_webdav_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


def test_every_tool_returning_a_linkable_model_applies_with_links():
    missing = []
    for name, tool in _all_tools().items():
        if name in EXEMPT:
            continue
        return_annotation = inspect.signature(tool.fn).return_annotation
        if not _linkable_models(return_annotation):
            continue
        if not getattr(tool.fn, "__with_links__", False):
            missing.append(name)

    assert not missing, (
        "these tools return a model that can carry a deep link but do not apply "
        f"@with_links, so their url field is always None: {sorted(missing)}. Add "
        "the decorator, or add the tool to EXEMPT with a reason."
    )


def test_exempt_tools_really_do_return_a_linkable_model():
    """Keeps EXEMPT honest — a stale entry would hide a real gap."""
    tools = _all_tools()
    for name, reason in EXEMPT.items():
        assert name in tools, f"EXEMPT lists unknown tool {name!r}"
        annotation = inspect.signature(tools[name].fn).return_annotation
        assert _linkable_models(annotation), (
            f"{name} is exempted ({reason}) but returns nothing linkable — "
            "the entry is stale and should be removed."
        )
        assert not getattr(tools[name].fn, "__with_links__", False), (
            f"{name} is exempted ({reason}) but does apply @with_links"
        )


def test_the_marker_survives_the_outer_decorators():
    """The whole check rests on `__with_links__` being visible on the final tool.

    `functools.wraps` copies `__dict__`, so an outer decorator (`@require_scopes`)
    carries the flag up. If that ever stopped holding, the coverage test above
    would silently pass for every tool.
    """
    tools = _all_tools()
    assert getattr(tools["nc_notes_get_note"].fn, "__with_links__", False)
