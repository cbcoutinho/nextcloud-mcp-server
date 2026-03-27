"""Unit tests for CollectivesClient API methods."""

import httpx
import pytest

from nextcloud_mcp_server.client.collectives import CollectivesClient, OCSError
from tests.client.conftest import create_mock_response

pytestmark = pytest.mark.unit


# --- OCS mock helpers ---


def _ocs_response(data: dict | list) -> httpx.Response:
    """Wrap data in an OCS envelope and return as mock response."""
    return create_mock_response(
        status_code=200,
        json_data={"ocs": {"meta": {"status": "ok", "statuscode": 200}, "data": data}},
    )


def _sample_collective(
    collective_id: int = 1, name: str = "Test Wiki", emoji: str | None = None
) -> dict:
    return {
        "id": collective_id,
        "circleId": "circle-abc",
        "emoji": emoji,
        "name": name,
        "level": 9,
        "canEdit": True,
        "canShare": True,
        "pageMode": 0,
    }


def _sample_page(
    page_id: int = 10,
    title: str = "Test Page",
    parent_id: int = 0,
    collective_path: str = "Collectives/Test Wiki",
) -> dict:
    return {
        "id": page_id,
        "title": title,
        "emoji": None,
        "fileName": f"{title}.md",
        "filePath": "",
        "collectivePath": collective_path,
        "parentId": parent_id,
        "timestamp": 1700000000,
        "size": 42,
        "lastUserId": "testuser",
        "lastUserDisplayName": "Test User",
        "subpageOrder": [],
        "isFullWidth": False,
    }


def _sample_tag(
    tag_id: int = 1, name: str = "important", color: str = "FF0000"
) -> dict:
    return {
        "id": tag_id,
        "collectiveId": 1,
        "name": name,
        "color": color,
    }


# --- Collectives ---


async def test_get_collectives(mocker):
    """Test listing collectives unwraps OCS envelope correctly."""
    mock_response = _ocs_response(
        {
            "collectives": [
                _sample_collective(1, "Wiki A"),
                _sample_collective(2, "Wiki B"),
            ]
        }
    )
    mocker.patch.object(CollectivesClient, "_make_request", return_value=mock_response)

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.get_collectives()

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["id"] == 1
    assert result[1]["name"] == "Wiki B"


async def test_create_collective(mocker):
    """Test creating a collective sends name and emoji."""
    mock_response = _ocs_response(
        {"collective": _sample_collective(5, "New Wiki", "📚")}
    )
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.create_collective("New Wiki", emoji="📚")

    assert result["id"] == 5
    assert result["name"] == "New Wiki"
    assert result["emoji"] == "📚"

    call_args = mock_request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[1]["json"]["name"] == "New Wiki"
    assert call_args[1]["json"]["emoji"] == "📚"


async def test_trash_collective(mocker):
    """Test trashing a collective sends DELETE to correct endpoint."""
    mock_response = _ocs_response({})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.trash_collective(collective_id=5)

    call_args = mock_request.call_args
    assert call_args[0][0] == "DELETE"
    assert "/collectives/5" in call_args[0][1]
    assert "/trash" not in call_args[0][1]


async def test_delete_collective(mocker):
    """Test permanently deleting a collective sends DELETE to trash endpoint."""
    mock_response = _ocs_response({})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.delete_collective(collective_id=5)

    call_args = mock_request.call_args
    assert call_args[0][0] == "DELETE"
    assert "/collectives/trash/5" in call_args[0][1]


# --- Pages ---


async def test_get_pages(mocker):
    """Test listing pages in a collective."""
    mock_response = _ocs_response(
        {"pages": [_sample_page(10, "Page A"), _sample_page(20, "Page B")]}
    )
    mocker.patch.object(CollectivesClient, "_make_request", return_value=mock_response)

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.get_pages(collective_id=1)

    assert len(result) == 2
    assert result[0]["title"] == "Page A"
    assert result[1]["id"] == 20


async def test_get_page(mocker):
    """Test getting a single page metadata."""
    mock_response = _ocs_response({"page": _sample_page(10, "My Page")})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.get_page(collective_id=1, page_id=10)

    assert result["id"] == 10
    assert result["title"] == "My Page"
    assert result["collectivePath"] == "Collectives/Test Wiki"

    call_args = mock_request.call_args
    assert "/collectives/1/pages/10" in call_args[0][1]


async def test_create_page(mocker):
    """Test creating a page under a parent."""
    mock_response = _ocs_response({"page": _sample_page(30, "New Page", parent_id=10)})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.create_page(collective_id=1, parent_id=10, title="New Page")

    assert result["id"] == 30
    assert result["parentId"] == 10

    call_args = mock_request.call_args
    assert call_args[0][0] == "POST"
    assert "/collectives/1/pages/10" in call_args[0][1]
    assert call_args[1]["json"]["title"] == "New Page"


async def test_trash_page(mocker):
    """Test trashing a page sends DELETE."""
    mock_response = _ocs_response({})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.trash_page(collective_id=1, page_id=10)

    call_args = mock_request.call_args
    assert call_args[0][0] == "DELETE"
    assert "/collectives/1/pages/10" in call_args[0][1]


async def test_move_page(mocker):
    """Test moving a page sends PUT with correct params."""
    mock_response = _ocs_response(
        {"page": _sample_page(10, "Moved Page", parent_id=20)}
    )
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.move_page(
        collective_id=1, page_id=10, parent_id=20, title="Moved Page"
    )

    assert result["parentId"] == 20
    call_args = mock_request.call_args
    assert call_args[0][0] == "PUT"
    assert call_args[1]["json"]["parentId"] == 20


# --- Search ---


async def test_search_pages(mocker):
    """Test full-text search sends query parameter."""
    mock_response = _ocs_response({"pages": [_sample_page(10, "Result Page")]})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.search_pages(collective_id=1, query="test query")

    assert len(result) == 1
    assert result[0]["title"] == "Result Page"

    call_args = mock_request.call_args
    assert call_args[1]["params"]["searchString"] == "test query"


# --- Tags ---


async def test_get_tags(mocker):
    """Test listing tags."""
    mock_response = _ocs_response(
        {"tags": [_sample_tag(1, "important"), _sample_tag(2, "draft", "00FF00")]}
    )
    mocker.patch.object(CollectivesClient, "_make_request", return_value=mock_response)

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.get_tags(collective_id=1)

    assert len(result) == 2
    assert result[0]["name"] == "important"
    assert result[1]["color"] == "00FF00"


async def test_create_tag(mocker):
    """Test creating a tag."""
    mock_response = _ocs_response({"tag": _sample_tag(3, "review", "0000FF")})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.create_tag(collective_id=1, name="review", color="0000FF")

    assert result["id"] == 3
    assert result["name"] == "review"

    call_args = mock_request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[1]["json"]["name"] == "review"


async def test_assign_tag(mocker):
    """Test assigning a tag to a page."""
    mock_response = _ocs_response({})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.assign_tag(collective_id=1, page_id=10, tag_id=3)

    call_args = mock_request.call_args
    assert call_args[0][0] == "PUT"
    assert "/pages/10/tags/3" in call_args[0][1]


async def test_remove_tag(mocker):
    """Test removing a tag from a page."""
    mock_response = _ocs_response({})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    await client.remove_tag(collective_id=1, page_id=10, tag_id=3)

    call_args = mock_request.call_args
    assert call_args[0][0] == "DELETE"
    assert "/pages/10/tags/3" in call_args[0][1]


# --- Trash ---


async def test_get_trashed_pages(mocker):
    """Test listing trashed pages."""
    trashed = _sample_page(10, "Trashed Page")
    trashed["trashTimestamp"] = 1700000000
    mock_response = _ocs_response({"pages": [trashed]})
    mocker.patch.object(CollectivesClient, "_make_request", return_value=mock_response)

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.get_trashed_pages(collective_id=1)

    assert len(result) == 1
    assert result[0]["title"] == "Trashed Page"


async def test_restore_page(mocker):
    """Test restoring a page from trash."""
    mock_response = _ocs_response({"page": _sample_page(10, "Restored Page")})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.restore_page(collective_id=1, page_id=10)

    assert result["title"] == "Restored Page"
    call_args = mock_request.call_args
    assert call_args[0][0] == "PATCH"
    assert "/pages/trash/10" in call_args[0][1]


# --- Error Handling ---


async def test_ocs_missing_data_raises_ocs_error(mocker):
    """Test that OCS envelope without 'data' key raises OCSError."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={
            "ocs": {
                "meta": {"status": "ok", "statuscode": 200},
            }
        },
    )
    mocker.patch.object(CollectivesClient, "_make_request", return_value=mock_response)

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(OCSError, match="missing 'data' field"):
        await client.get_collectives()


async def test_non_ocs_envelope_raises_ocs_error(mocker):
    """Test that a non-OCS response (e.g. proxy error) raises OCSError."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={"error": "Bad Gateway"},
    )
    mocker.patch.object(CollectivesClient, "_make_request", return_value=mock_response)

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(OCSError, match="not an OCS envelope"):
        await client.get_collectives()


async def test_ocs_error_status_raises(mocker):
    """Test that OCS envelope with error statuscode raises OCSError."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={
            "ocs": {
                "meta": {
                    "status": "failure",
                    "statuscode": 403,
                    "message": "Not permitted",
                },
                "data": {},
            }
        },
    )
    mocker.patch.object(CollectivesClient, "_make_request", return_value=mock_response)

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(OCSError, match="Not permitted"):
        await client.get_collectives()


async def test_get_collectives_403(mocker):
    """Test 403 response raises HTTPStatusError."""
    mock_response = create_mock_response(
        status_code=403, json_data={"message": "Forbidden"}
    )
    mock_response.raise_for_status = lambda: (_ for _ in ()).throw(
        httpx.HTTPStatusError(
            "Forbidden", request=mock_response.request, response=mock_response
        )
    )
    mocker.patch.object(
        CollectivesClient,
        "_make_request",
        side_effect=httpx.HTTPStatusError(
            "Forbidden",
            request=httpx.Request("GET", "http://test"),
            response=mock_response,
        ),
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_collectives()


async def test_get_page_404(mocker):
    """Test 404 response raises HTTPStatusError."""
    mock_response = create_mock_response(
        status_code=404, json_data={"message": "Not found"}
    )
    mocker.patch.object(
        CollectivesClient,
        "_make_request",
        side_effect=httpx.HTTPStatusError(
            "Not Found",
            request=httpx.Request("GET", "http://test"),
            response=mock_response,
        ),
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_page(collective_id=1, page_id=999)


# --- Additional coverage ---


async def test_update_collective_no_fields_raises_value_error(mocker):
    """Test that update_collective raises ValueError when called with no fields."""
    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(ValueError, match="At least one field"):
        await client.update_collective(collective_id=1)


async def test_set_page_emoji_clear(mocker):
    """Test that set_page_emoji sends null emoji to clear it."""
    page = _sample_page(10, "Test Page")
    page["emoji"] = None
    mock_response = _ocs_response({"page": page})
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.set_page_emoji(collective_id=1, page_id=10, emoji=None)

    assert result["emoji"] is None
    call_args = mock_request.call_args
    assert call_args[1]["json"] == {"emoji": None}


async def test_get_trashed_collectives(mocker):
    """Test listing trashed collectives."""
    mock_response = _ocs_response(
        {"collectives": [_sample_collective(1, "Trashed Wiki")]}
    )
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.get_trashed_collectives()

    assert len(result) == 1
    assert result[0]["name"] == "Trashed Wiki"
    call_args = mock_request.call_args
    assert "/collectives/trash" in call_args[0][1]
    assert call_args[0][0] == "GET"


async def test_restore_collective(mocker):
    """Test restoring a collective from trash."""
    mock_response = _ocs_response(
        {"collective": _sample_collective(5, "Restored Wiki")}
    )
    mock_request = mocker.patch.object(
        CollectivesClient, "_make_request", return_value=mock_response
    )

    client = CollectivesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.restore_collective(collective_id=5)

    assert result["name"] == "Restored Wiki"
    call_args = mock_request.call_args
    assert call_args[0][0] == "PATCH"
    assert "/collectives/trash/5" in call_args[0][1]
