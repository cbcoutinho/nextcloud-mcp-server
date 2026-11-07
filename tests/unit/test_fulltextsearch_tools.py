"""Server-layer tests for the Full Text Search tool.

These register the tool on a fresh ``MCPServer`` and invoke its function directly.
The point is what the client-layer tests can't see: validation, the mapping of hits,
the tag exclusion and the translation of errors.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.client.fulltextsearch import FullTextSearchError
from nextcloud_mcp_server.server import APP_CAPABILITY_KEY, AVAILABLE_APPS
from nextcloud_mcp_server.server.fulltextsearch import configure_fulltextsearch_tools

pytestmark = pytest.mark.unit

MODULE = "nextcloud_mcp_server.server.fulltextsearch"


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path."""
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def search():
    mcp = MCPServer(name="test-fulltextsearch-tools")
    configure_fulltextsearch_tools(mcp)
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    return tools["nc_fulltextsearch_search_files"].fn


@pytest.fixture
def client(mocker):
    client = SimpleNamespace(fulltextsearch=AsyncMock(), webdav=AsyncMock())

    async def fake_get_client(ctx):
        return client

    mocker.patch(f"{MODULE}.get_client", side_effect=fake_get_client)
    mocker.patch(f"{MODULE}.get_excluded_file_paths", return_value=set())
    return client


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace())


def _document(path, **info):
    return {
        "info": {"path": path, **info},
        "excerpts": [{"source": "content", "excerpt": " the invoice "}],
    }


def test_registered_and_gated_on_the_app():
    assert AVAILABLE_APPS["fulltextsearch"] is configure_fulltextsearch_tools
    assert APP_CAPABILITY_KEY["fulltextsearch"] == "fulltextsearch"


async def test_maps_hits(search, client):
    client.fulltextsearch.search_files.return_value = (
        3,
        [_document("/dir/a.pdf", mime="application/pdf", size=10, mtime=1700000000)],
    )

    result = await search("invoice", _ctx(), limit=1)

    assert (result.total, result.has_more, result.note) == (3, True, None)
    hit = result.results[0]
    assert hit.path == "dir/a.pdf"
    assert (hit.mime_type, hit.size) == ("application/pdf", 10)
    assert hit.modified == "2023-11-14T22:13:20+00:00"
    assert hit.excerpts == ["the invoice"]


async def test_limits_the_excerpts(search, client):
    document = _document("/a.txt")
    document["excerpts"] = [{"excerpt": "x" * 1000}] * 5
    client.fulltextsearch.search_files.return_value = (1, [document])

    result = await search("invoice", _ctx())

    assert [len(e) for e in result.results[0].excerpts] == [300, 300, 300]


async def test_hides_excluded_paths(search, client, mocker):
    mocker.patch(f"{MODULE}.get_excluded_file_paths", return_value={"secret"})
    client.fulltextsearch.search_files.return_value = (
        2,
        [_document("/secret/a.pdf"), _document("/open/b.pdf")],
    )

    result = await search("invoice", _ctx())

    assert [h.path for h in result.results] == ["open/b.pdf"]


async def test_last_page_has_no_more(search, client):
    client.fulltextsearch.search_files.return_value = (40, [_document("/a.pdf")])

    assert (await search("invoice", _ctx(), limit=20, page=2)).has_more is False


async def test_explains_hits_without_documents(search, client):
    client.fulltextsearch.search_files.return_value = (5, [])

    result = await search("invoice", _ctx())

    assert result.results == []
    assert "files_fulltextsearch" in result.note


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "  "},
        {"query": "x", "extension": "p.*"},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 51},
        {"query": "x", "page": 0},
    ],
)
async def test_rejects_invalid_arguments(search, client, kwargs):
    kwargs = dict(kwargs)
    with pytest.raises(ToolError):
        await search(kwargs.pop("query"), _ctx(), **kwargs)
    client.fulltextsearch.search_files.assert_not_awaited()


async def test_strips_the_dot_of_an_extension(search, client):
    client.fulltextsearch.search_files.return_value = (0, [])

    await search("invoice", _ctx(), extension=".pdf")

    assert client.fulltextsearch.search_files.await_args.kwargs["extension"] == "pdf"


@pytest.mark.parametrize(
    ("status", "message"), [(404, "not available"), (500, "HTTP 500")]
)
async def test_translates_http_errors(search, client, status, message):
    response = httpx2.Response(status, request=httpx2.Request("GET", "http://nc/"))
    client.fulltextsearch.search_files.side_effect = httpx2.HTTPStatusError(
        "failed", request=response.request, response=response
    )

    with pytest.raises(ToolError, match=message):
        await search("invoice", _ctx())


async def test_translates_failed_status(search, client):
    client.fulltextsearch.search_files.side_effect = FullTextSearchError("no platform")

    with pytest.raises(ToolError, match="no platform"):
        await search("invoice", _ctx())
