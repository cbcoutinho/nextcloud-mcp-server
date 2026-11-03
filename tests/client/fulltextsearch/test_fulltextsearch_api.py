import json
import logging

import httpx
import pytest

from nextcloud_mcp_server.client.fulltextsearch import FullTextSearchClient
from tests.client.conftest import create_mock_response

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.unit


async def test_search_builds_request_param(mocker):
    """The `request` query param carries the JSON-encoded SearchRequest."""
    mock_response = create_mock_response(
        status_code=200, json_data={"status": 1, "result": []}
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        FullTextSearchClient, "_make_request", return_value=mock_response
    )

    client = FullTextSearchClient(mock_client, "testuser")
    results = await client.search("quarterly report", providers=["files"], size=5)

    assert results == []
    args, kwargs = mock_make_request.call_args
    assert args == ("GET", "/apps/fulltextsearch/v1/remote")
    sent = json.loads(kwargs["params"]["request"])
    assert sent == {
        "providers": ["files"],
        "search": "quarterly report",
        "page": 1,
        "size": 5,
    }


async def test_search_defaults_providers_to_all_sentinel(mocker):
    """An unset provider filter must serialize as ``["all"]``, never ``[]``.

    Regression guard: ``ProviderService::getFilteredProviders()`` unions only
    the *named* providers unless the list contains the literal string ``all``
    — so a literal empty list selects ZERO providers and every search silently
    answers HTTP 200 / status 1 with no hits (found live against FTS 32.0.0;
    the web UI found results for the same term the tool could not).
    """
    mock_response = create_mock_response(
        status_code=200, json_data={"status": 1, "result": []}
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        FullTextSearchClient, "_make_request", return_value=mock_response
    )

    client = FullTextSearchClient(mock_client, "testuser")
    await client.search("tracker 1234")

    _, kwargs = mock_make_request.call_args
    sent = json.loads(kwargs["params"]["request"])
    assert sent["providers"] == ["all"]


async def test_search_flattens_provider_grouped_results(mocker):
    """Grouped `{provider: {entries: [...]}}` results flatten to hit dicts."""
    payload = {
        "status": 1,
        "result": {
            "files_fulltextsearch": {
                "entries": [
                    {
                        "title": "budget.xlsx",
                        "sub_title": "Documents",
                        "score": 4.2,
                        "route": {"url": "https://nc/f/123"},
                        "attributes": {
                            "path": "Docs/budget.xlsx",
                            "mime": "application/vnd.ms-excel",
                        },
                    }
                ]
            }
        },
    }
    mock_response = create_mock_response(status_code=200, json_data=payload)
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mocker.patch.object(
        FullTextSearchClient, "_make_request", return_value=mock_response
    )

    client = FullTextSearchClient(mock_client, "testuser")
    results = await client.search("budget")

    assert len(results) == 1
    hit = results[0]
    assert hit["provider"] == "files_fulltextsearch"
    assert hit["title"] == "budget.xlsx"
    assert hit["path"] == "Docs/budget.xlsx"
    assert hit["url"] == "https://nc/f/123"
    assert hit["subtitle"] == "Documents"
    assert hit["score"] == pytest.approx(4.2)
    assert hit["attributes"]["mime"] == "application/vnd.ms-excel"


async def test_search_returns_empty_on_error_status(mocker):
    """A status != 1 envelope yields no hits rather than raising."""
    payload = {"status": -1, "message": "no platform configured"}
    mock_response = create_mock_response(status_code=200, json_data=payload)
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mocker.patch.object(
        FullTextSearchClient, "_make_request", return_value=mock_response
    )

    client = FullTextSearchClient(mock_client, "testuser")
    assert await client.search("anything") == []


def test_flatten_parses_live_fts32_documents_shape():
    """Parse the real FTS >=30 envelope captured live from a running instance.

    The response is a list of per-provider groups whose hits sit under
    ``documents`` (not ``entries``), with metadata under ``info``, a ``link``
    browser url, string scores and ``excerpts`` snippets — none of which the
    originally-assumed shape matched, so results parsed to empty rows.
    """
    payload = {
        "status": 1,
        "result": [
            {
                "provider": {"id": "deck", "name": "Deck"},
                "platform": {"id": "sql", "name": "Nextcloud database (SQL)"},
                "documents": [],
                "info": [],
                "meta": {"timedOut": False, "time": 1, "count": 0},
            },
            {
                "provider": {"id": "files", "name": "Files"},
                "platform": {"id": "sql", "name": "Nextcloud database (SQL)"},
                "documents": [
                    {
                        "id": "5843189",
                        "providerId": "files",
                        "title": "Media/Scans/scan.jpg",
                        "link": "/index.php/f/5843189",
                        "info": {
                            "path": "/Media/Scans/scan.jpg",
                            "mime": "image/jpeg",
                            "size": 408738,
                            "unified": {"icon": "/icon.svg"},
                        },
                        "excerpts": [
                            {
                                "source": "TRACKNO123",
                                "excerpt": " observatie ©  trackno123",
                            }
                        ],
                        "score": "27.20055770874",
                    }
                ],
            },
        ],
        "version": "32.0.0",
    }

    hits = FullTextSearchClient._flatten(payload["result"])

    assert len(hits) == 1
    hit = hits[0]
    assert hit["provider"] == "files"
    assert hit["title"] == "Media/Scans/scan.jpg"
    assert hit["path"] == "/Media/Scans/scan.jpg"
    assert hit["url"] == "/index.php/f/5843189"
    assert hit["reference"] == "5843189"
    assert hit["subtitle"] == "observatie ©  trackno123"
    assert hit["score"] == pytest.approx(27.20055770874)
    assert hit["attributes"]["mime"] == "image/jpeg"


def test_flatten_handles_list_and_single_group_forms():
    """The parser tolerates the list-of-groups and single-group envelopes."""
    single = {"entries": [{"title": "a.txt", "attributes": {"source": "/a.txt"}}]}
    assert FullTextSearchClient._flatten(single)[0]["path"] == "/a.txt"

    as_list = [{"provider": "files", "entries": [{"title": "b.txt", "url": "u"}]}]
    hits = FullTextSearchClient._flatten(as_list)
    assert hits[0]["provider"] == "files"
    assert hits[0]["url"] == "u"

    assert FullTextSearchClient._flatten(None) == []
