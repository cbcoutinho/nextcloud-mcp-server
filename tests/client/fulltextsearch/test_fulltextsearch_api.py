"""Mocked unit tests for the Full Text Search client.

What these pin down is the request the app needs and the shape of its answer:
the whole request travels as one JSON parameter, and the hits are grouped by provider.
"""

import json

import httpx2
import pytest

from nextcloud_mcp_server.client.fulltextsearch import (
    FullTextSearchClient,
    FullTextSearchError,
)
from tests.client.conftest import create_mock_response

pytestmark = pytest.mark.unit


def _client(mocker, payload):
    mock_make_request = mocker.patch.object(
        FullTextSearchClient,
        "_make_request",
        return_value=create_mock_response(status_code=200, json_data=payload),
    )
    client = FullTextSearchClient(mocker.AsyncMock(spec=httpx2.AsyncClient), "testuser")
    return client, mock_make_request


def _answer(*groups):
    return {"status": 1, "result": list(groups)}


def _group(provider, total, documents):
    return {
        "provider": {"id": provider},
        "meta": {"total": total},
        "documents": documents,
    }


def _request_of(mock_make_request):
    return json.loads(mock_make_request.call_args.kwargs["params"]["request"])


async def test_search_sends_one_json_request_parameter(mocker):
    client, mock_make_request = _client(mocker, _answer(_group("files", 0, [])))

    await client.search_files("invoice", limit=5, page=2)

    assert mock_make_request.call_args.args == (
        "GET",
        "/apps/fulltextsearch/v1/remote",
    )
    request = _request_of(mock_make_request)
    assert request["providers"] == ["files"]
    assert request["search"] == "invoice"
    assert (request["page"], request["size"]) == (2, 5)
    assert request["options"] == {}


async def test_extension_matches_either_case(mocker):
    client, mock_make_request = _client(mocker, _answer(_group("files", 0, [])))

    await client.search_files("invoice", extension="pdf2")

    options = _request_of(mock_make_request)["options"]
    assert options["files_extension"] == "[pP][dD][fF]2"


@pytest.mark.parametrize("match", ["content", "filename"])
async def test_match_limits_the_searched_parts(mocker, match):
    client, mock_make_request = _client(mocker, _answer(_group("files", 0, [])))

    await client.search_files("invoice", match=match)

    assert _request_of(mock_make_request)["options"]["in"] == [match]


async def test_returns_total_and_documents_of_the_files_provider(mocker):
    document = {"info": {"path": "/a.pdf"}}
    client, _ = _client(
        mocker,
        _answer(
            _group("deck", 7, [{"title": "card"}]), _group("files", 42, [document])
        ),
    )

    assert await client.search_files("invoice") == (42, [document])


async def test_no_group_for_files_means_no_hits(mocker):
    client, _ = _client(mocker, _answer())

    assert await client.search_files("invoice") == (0, [])


async def test_failed_status_raises(mocker):
    client, _ = _client(mocker, {"status": -1, "message": "no platform"})

    with pytest.raises(FullTextSearchError, match="no platform"):
        await client.search_files("invoice")
