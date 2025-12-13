"""Unit tests for NewsClient API methods."""

import logging

import httpx
import pytest

from nextcloud_mcp_server.client.news import NewsClient, NewsItemType
from tests.client.conftest import (
    create_mock_error_response,
    create_mock_news_feed_response,
    create_mock_news_feeds_response,
    create_mock_news_folder_response,
    create_mock_news_folders_response,
    create_mock_news_item,
    create_mock_news_items_response,
    create_mock_news_status_response,
    create_mock_response,
)

logger = logging.getLogger(__name__)

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


# ============================================================================
# Folder Tests
# ============================================================================


async def test_news_api_get_folders(mocker):
    """Test that get_folders correctly parses the API response."""
    mock_response = create_mock_news_folders_response(
        folders=[
            {"id": 1, "name": "Tech"},
            {"id": 2, "name": "News"},
        ]
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    folders = await client.get_folders()

    assert len(folders) == 2
    assert folders[0]["id"] == 1
    assert folders[0]["name"] == "Tech"
    assert folders[1]["name"] == "News"

    mock_make_request.assert_called_once_with("GET", "/apps/news/api/v1-3/folders")


async def test_news_api_create_folder(mocker):
    """Test that create_folder correctly creates a folder."""
    mock_response = create_mock_news_folder_response(folder_id=3, name="New Folder")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    folder = await client.create_folder(name="New Folder")

    assert folder["id"] == 3
    assert folder["name"] == "New Folder"

    mock_make_request.assert_called_once_with(
        "POST", "/apps/news/api/v1-3/folders", json={"name": "New Folder"}
    )


async def test_news_api_rename_folder(mocker):
    """Test that rename_folder makes the correct API call."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.rename_folder(folder_id=1, name="Renamed")

    mock_make_request.assert_called_once_with(
        "PUT", "/apps/news/api/v1-3/folders/1", json={"name": "Renamed"}
    )


async def test_news_api_delete_folder(mocker):
    """Test that delete_folder makes the correct API call."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.delete_folder(folder_id=1)

    mock_make_request.assert_called_once_with("DELETE", "/apps/news/api/v1-3/folders/1")


# ============================================================================
# Feed Tests
# ============================================================================


async def test_news_api_get_feeds(mocker):
    """Test that get_feeds correctly parses the API response."""
    mock_response = create_mock_news_feeds_response(
        feeds=[
            {"id": 1, "url": "https://example.com/feed1", "title": "Feed 1"},
            {"id": 2, "url": "https://example.com/feed2", "title": "Feed 2"},
        ],
        starred_count=5,
        newest_item_id=100,
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    result = await client.get_feeds()

    assert len(result["feeds"]) == 2
    assert result["starredCount"] == 5
    assert result["newestItemId"] == 100

    mock_make_request.assert_called_once_with("GET", "/apps/news/api/v1-3/feeds")


async def test_news_api_create_feed(mocker):
    """Test that create_feed correctly creates a feed."""
    mock_response = create_mock_news_feed_response(
        feed_id=10, url="https://example.com/new-feed", title="New Feed"
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    feed = await client.create_feed(url="https://example.com/new-feed")

    assert feed["id"] == 10
    assert feed["url"] == "https://example.com/new-feed"

    mock_make_request.assert_called_once_with(
        "POST",
        "/apps/news/api/v1-3/feeds",
        json={"url": "https://example.com/new-feed"},
    )


async def test_news_api_create_feed_with_folder(mocker):
    """Test that create_feed correctly creates a feed in a folder."""
    mock_response = create_mock_news_feed_response(
        feed_id=10, url="https://example.com/feed", folder_id=5
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    feed = await client.create_feed(url="https://example.com/feed", folder_id=5)

    assert feed["folderId"] == 5

    mock_make_request.assert_called_once_with(
        "POST",
        "/apps/news/api/v1-3/feeds",
        json={"url": "https://example.com/feed", "folderId": 5},
    )


async def test_news_api_delete_feed(mocker):
    """Test that delete_feed makes the correct API call."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.delete_feed(feed_id=10)

    mock_make_request.assert_called_once_with("DELETE", "/apps/news/api/v1-3/feeds/10")


async def test_news_api_move_feed(mocker):
    """Test that move_feed makes the correct API call."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.move_feed(feed_id=10, folder_id=5)

    mock_make_request.assert_called_once_with(
        "POST", "/apps/news/api/v1-3/feeds/10/move", json={"folderId": 5}
    )


async def test_news_api_rename_feed(mocker):
    """Test that rename_feed makes the correct API call."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.rename_feed(feed_id=10, title="Renamed Feed")

    mock_make_request.assert_called_once_with(
        "POST",
        "/apps/news/api/v1-3/feeds/10/rename",
        json={"feedTitle": "Renamed Feed"},
    )


# ============================================================================
# Item Tests
# ============================================================================


async def test_news_api_get_items(mocker):
    """Test that get_items correctly parses the API response."""
    items = [
        create_mock_news_item(item_id=1, title="Article 1"),
        create_mock_news_item(item_id=2, title="Article 2"),
    ]
    mock_response = create_mock_news_items_response(items=items)

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    result = await client.get_items()

    assert len(result) == 2
    assert result[0]["title"] == "Article 1"
    assert result[1]["title"] == "Article 2"

    # Verify default parameters
    call_args = mock_make_request.call_args
    assert call_args[0] == ("GET", "/apps/news/api/v1-3/items")
    params = call_args[1]["params"]
    assert params["batchSize"] == 50
    assert params["type"] == NewsItemType.ALL


async def test_news_api_get_items_starred(mocker):
    """Test that get_items with STARRED type filters correctly."""
    items = [create_mock_news_item(item_id=1, starred=True)]
    mock_response = create_mock_news_items_response(items=items)

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    result = await client.get_items(type_=NewsItemType.STARRED)

    assert len(result) == 1
    assert result[0]["starred"] is True

    call_args = mock_make_request.call_args
    params = call_args[1]["params"]
    assert params["type"] == NewsItemType.STARRED


async def test_news_api_get_items_unread_only(mocker):
    """Test that get_items with get_read=False filters correctly."""
    items = [create_mock_news_item(item_id=1, unread=True)]
    mock_response = create_mock_news_items_response(items=items)

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    result = await client.get_items(get_read=False)

    assert len(result) == 1

    call_args = mock_make_request.call_args
    params = call_args[1]["params"]
    assert params["getRead"] == "false"


async def test_news_api_get_item(mocker):
    """Test that get_item fetches all items and filters for the requested ID."""
    # Create multiple items, only one should be returned
    items = [
        create_mock_news_item(item_id=100, title="Other Item 1"),
        create_mock_news_item(item_id=123, title="Single Item"),
        create_mock_news_item(item_id=200, title="Other Item 2"),
    ]

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_get_items = mocker.patch.object(NewsClient, "get_items", return_value=items)

    client = NewsClient(mock_client, "testuser")
    result = await client.get_item(item_id=123)

    assert result["id"] == 123
    assert result["title"] == "Single Item"

    # Verify it fetched all items with correct params
    mock_get_items.assert_called_once_with(batch_size=-1, get_read=True)


async def test_news_api_get_item_not_found(mocker):
    """Test that get_item raises ValueError when item not found."""
    items = [
        create_mock_news_item(item_id=100, title="Item 1"),
        create_mock_news_item(item_id=200, title="Item 2"),
    ]

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mocker.patch.object(NewsClient, "get_items", return_value=items)

    client = NewsClient(mock_client, "testuser")

    with pytest.raises(ValueError, match="Item 999 not found"):
        await client.get_item(item_id=999)


async def test_news_api_get_updated_items(mocker):
    """Test that get_updated_items correctly calls the updated endpoint."""
    items = [create_mock_news_item(item_id=1)]
    mock_response = create_mock_news_items_response(items=items)

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    result = await client.get_updated_items(last_modified=1700000000)

    assert len(result) == 1

    call_args = mock_make_request.call_args
    assert call_args[0] == ("GET", "/apps/news/api/v1-3/items/updated")
    params = call_args[1]["params"]
    assert params["lastModified"] == 1700000000


async def test_news_api_mark_item_read(mocker):
    """Test that mark_item_read makes the correct API call."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.mark_item_read(item_id=123)

    mock_make_request.assert_called_once_with(
        "POST", "/apps/news/api/v1-3/items/123/read"
    )


async def test_news_api_mark_item_unread(mocker):
    """Test that mark_item_unread makes the correct API call."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.mark_item_unread(item_id=123)

    mock_make_request.assert_called_once_with(
        "POST", "/apps/news/api/v1-3/items/123/unread"
    )


async def test_news_api_star_item(mocker):
    """Test that star_item makes the correct API call."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.star_item(item_id=123)

    mock_make_request.assert_called_once_with(
        "POST", "/apps/news/api/v1-3/items/123/star"
    )


async def test_news_api_unstar_item(mocker):
    """Test that unstar_item makes the correct API call."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.unstar_item(item_id=123)

    mock_make_request.assert_called_once_with(
        "POST", "/apps/news/api/v1-3/items/123/unstar"
    )


async def test_news_api_mark_items_read_multiple(mocker):
    """Test that mark_items_read makes the correct API call for multiple items."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.mark_items_read(item_ids=[1, 2, 3])

    mock_make_request.assert_called_once_with(
        "POST", "/apps/news/api/v1-3/items/read/multiple", json={"itemIds": [1, 2, 3]}
    )


async def test_news_api_star_items_multiple(mocker):
    """Test that star_items makes the correct API call for multiple items."""
    mock_response = create_mock_response(status_code=200, json_data={})

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    await client.star_items(item_ids=[1, 2, 3])

    mock_make_request.assert_called_once_with(
        "POST", "/apps/news/api/v1-3/items/star/multiple", json={"itemIds": [1, 2, 3]}
    )


# ============================================================================
# Status Tests
# ============================================================================


async def test_news_api_get_status(mocker):
    """Test that get_status correctly parses the API response."""
    mock_response = create_mock_news_status_response(
        version="25.0.0",
        warnings={"improperlyConfiguredCron": False},
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    status = await client.get_status()

    assert status["version"] == "25.0.0"
    assert "warnings" in status

    mock_make_request.assert_called_once_with("GET", "/apps/news/api/v1-3/status")


async def test_news_api_get_version(mocker):
    """Test that get_version correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200, json_data={"version": "25.0.0"}
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NewsClient, "_make_request", return_value=mock_response
    )

    client = NewsClient(mock_client, "testuser")
    version = await client.get_version()

    assert version == "25.0.0"

    mock_make_request.assert_called_once_with("GET", "/apps/news/api/v1-3/version")


# ============================================================================
# Error Handling Tests
# ============================================================================


async def test_news_api_create_folder_conflict(mocker):
    """Test that create_folder raises HTTPStatusError on 409 conflict."""
    error_response = create_mock_error_response(409, "Folder name already exists")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(NewsClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "409 Conflict",
        request=httpx.Request("POST", "http://test.local"),
        response=error_response,
    )

    client = NewsClient(mock_client, "testuser")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.create_folder(name="Existing Folder")

    assert excinfo.value.response.status_code == 409


async def test_news_api_delete_feed_not_found(mocker):
    """Test that delete_feed raises HTTPStatusError on 404."""
    error_response = create_mock_error_response(404, "Feed not found")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(NewsClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "404 Not Found",
        request=httpx.Request("DELETE", "http://test.local"),
        response=error_response,
    )

    client = NewsClient(mock_client, "testuser")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.delete_feed(feed_id=999999)

    assert excinfo.value.response.status_code == 404


async def test_news_api_create_feed_invalid_url(mocker):
    """Test that create_feed raises HTTPStatusError on 422 for invalid URL."""
    error_response = create_mock_error_response(422, "Invalid feed URL")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(NewsClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "422 Unprocessable Entity",
        request=httpx.Request("POST", "http://test.local"),
        response=error_response,
    )

    client = NewsClient(mock_client, "testuser")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.create_feed(url="not-a-valid-url")

    assert excinfo.value.response.status_code == 422
