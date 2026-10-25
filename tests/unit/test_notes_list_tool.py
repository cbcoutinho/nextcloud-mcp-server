"""Unit tests for ``nc_notes_list_notes``.

The tool exists so an agent can answer "how many notes do I have": search tools
return best matches only, so ``total_found`` here must be the full count.
"""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.models.notes import ListNotesResponse
from nextcloud_mcp_server.server.notes import configure_notes_tools

pytestmark = pytest.mark.unit

NOTES = [
    {"id": 1, "title": "Groceries", "category": "Personal", "content": "milk"},
    {"id": 2, "title": "Runbook", "category": "Work", "content": "steps"},
    {"id": 3, "title": "Untitled", "content": "no category key"},
]


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path (see
    ``test_webdav_tools_exclusion.py`` for why this must be explicit)."""
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def list_notes(mocker):
    mcp = MCPServer(name="test-notes-tools")
    configure_notes_tools(mcp)
    tool = {t.name: t for t in mcp._tool_manager.list_tools()}["nc_notes_list_notes"]

    async def get_all_notes():
        for note in NOTES:
            yield note

    client = SimpleNamespace(notes=SimpleNamespace(get_all_notes=get_all_notes))

    async def fake_get_client(ctx):
        return client

    mocker.patch(
        "nextcloud_mcp_server.server.notes.get_client", side_effect=fake_get_client
    )
    return tool.fn


async def test_lists_every_note_and_counts_them(list_notes):
    result = await list_notes(ctx=SimpleNamespace(request_context=SimpleNamespace()))

    assert isinstance(result, ListNotesResponse)
    assert result.total_found == 3
    assert [n.id for n in result.results] == [1, 2, 3]
    assert result.results[2].category == ""
    assert result.category is None


async def test_limit_caps_results_but_not_the_count(list_notes):
    result = await list_notes(
        ctx=SimpleNamespace(request_context=SimpleNamespace()), limit=1
    )

    assert result.total_found == 3
    assert [n.id for n in result.results] == [1]


def test_count_serializes_before_results():
    """A client that truncates a long result must still see the count."""
    dumped = ListNotesResponse(total_found=0, results=[]).model_dump_json()
    assert dumped.index('"total_found"') < dumped.index('"results"')


async def test_access_denied_is_reported_as_mcp_error(mocker):
    mcp = MCPServer(name="test-notes-tools")
    configure_notes_tools(mcp)
    tool = {t.name: t for t in mcp._tool_manager.list_tools()}["nc_notes_list_notes"]

    async def get_all_notes():
        raise httpx.HTTPStatusError(
            "forbidden",
            request=httpx.Request("GET", "http://nc/notes"),
            response=httpx.Response(403),
        )
        yield  # pragma: no cover - makes this an async generator

    client = SimpleNamespace(notes=SimpleNamespace(get_all_notes=get_all_notes))

    async def fake_get_client(ctx):
        return client

    mocker.patch(
        "nextcloud_mcp_server.server.notes.get_client", side_effect=fake_get_client
    )
    ctx = SimpleNamespace(request_context=SimpleNamespace())

    with pytest.raises(MCPError, match="Access denied"):
        await tool.fn(ctx=ctx)


async def test_filters_by_category(list_notes):
    result = await list_notes(
        ctx=SimpleNamespace(request_context=SimpleNamespace()), category="Work"
    )

    assert result.total_found == 1
    assert result.results[0].title == "Runbook"
    assert result.category == "Work"
