"""Unit tests for WebDAV client."""

from unittest.mock import AsyncMock

import pytest

from nextcloud_mcp_server.client.webdav import WebDAVClient


@pytest.mark.unit
async def test_find_by_tag_calls_search_files(mocker):
    """Test that find_by_tag constructs correct search query."""
    # Create mock HTTP client
    mock_http_client = AsyncMock()

    # Create WebDAVClient instance
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock the search_files method to avoid actual HTTP calls
    mock_search_files = mocker.patch.object(client, "search_files", return_value=[])

    # Call find_by_tag
    await client.find_by_tag("vector-index")

    # Verify search_files was called with correct parameters
    mock_search_files.assert_called_once()
    call_args = mock_search_files.call_args

    # Check that the where_conditions contains the tag name
    assert "vector-index" in call_args.kwargs["where_conditions"]
    assert "<oc:tags/>" in call_args.kwargs["where_conditions"]
    assert "<d:like>" in call_args.kwargs["where_conditions"]

    # Check that tags property is requested
    assert "tags" in call_args.kwargs["properties"]


@pytest.mark.unit
async def test_find_by_tag_with_scope_and_limit(mocker):
    """Test find_by_tag passes scope and limit parameters."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    mock_search_files = mocker.patch.object(client, "search_files", return_value=[])

    # Call with scope and limit
    await client.find_by_tag("test-tag", scope="Documents", limit=10)

    # Verify parameters were passed through
    call_args = mock_search_files.call_args
    assert call_args.kwargs["scope"] == "Documents"
    assert call_args.kwargs["limit"] == 10


@pytest.mark.unit
def test_parse_search_response_with_tags(mocker):
    """Test that _parse_search_response correctly parses tags."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock XML response with tags (comma-separated format)
    xml_content = b"""<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
        <d:response>
            <d:href>/remote.php/dav/files/testuser/Documents/test.pdf</d:href>
            <d:propstat>
                <d:prop>
                    <d:displayname>test.pdf</d:displayname>
                    <d:getcontenttype>application/pdf</d:getcontenttype>
                    <d:getcontentlength>1024</d:getcontentlength>
                    <d:getetag>"abc123"</d:getetag>
                    <oc:fileid>12345</oc:fileid>
                    <oc:tags>vector-index,important</oc:tags>
                    <d:resourcetype/>
                </d:prop>
            </d:propstat>
        </d:response>
    </d:multistatus>"""

    # Parse the response
    results = client._parse_search_response(xml_content, scope="Documents")

    # Verify tags were parsed correctly
    assert len(results) == 1
    assert "tags" in results[0]
    assert results[0]["tags"] == ["vector-index", "important"]
    assert results[0]["name"] == "test.pdf"
    assert results[0]["content_type"] == "application/pdf"


@pytest.mark.unit
def test_parse_search_response_with_empty_tags(mocker):
    """Test that _parse_search_response handles files without tags."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock XML response without tags
    xml_content = b"""<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
        <d:response>
            <d:href>/remote.php/dav/files/testuser/Documents/test.txt</d:href>
            <d:propstat>
                <d:prop>
                    <d:displayname>test.txt</d:displayname>
                    <d:getcontenttype>text/plain</d:getcontenttype>
                    <oc:tags/>
                    <d:resourcetype/>
                </d:prop>
            </d:propstat>
        </d:response>
    </d:multistatus>"""

    # Parse the response
    results = client._parse_search_response(xml_content, scope="Documents")

    # Verify tags field is empty list
    assert len(results) == 1
    assert "tags" in results[0]
    assert results[0]["tags"] == []


@pytest.mark.unit
async def test_get_file_info_returns_file_details(mocker):
    """Test that get_file_info returns file info including file ID."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock PROPFIND response
    mock_response = AsyncMock()
    mock_response.status_code = 207
    mock_response.content = b"""<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
        <d:response>
            <d:href>/remote.php/dav/files/testuser/Documents/test.pdf</d:href>
            <d:propstat>
                <d:prop>
                    <oc:fileid>12345</oc:fileid>
                    <d:displayname>test.pdf</d:displayname>
                    <d:getcontentlength>1024</d:getcontentlength>
                    <d:getcontenttype>application/pdf</d:getcontenttype>
                    <d:getlastmodified>Sat, 01 Jan 2025 00:00:00 GMT</d:getlastmodified>
                    <d:getetag>"abc123"</d:getetag>
                    <d:resourcetype/>
                </d:prop>
            </d:propstat>
        </d:response>
    </d:multistatus>"""
    mock_response.raise_for_status = mocker.Mock()

    mock_http_client.request = AsyncMock(return_value=mock_response)

    # Call get_file_info
    result = await client.get_file_info("Documents/test.pdf")

    # Verify result
    assert result is not None
    assert result["id"] == 12345
    assert result["name"] == "test.pdf"
    assert result["path"] == "Documents/test.pdf"
    assert result["content_type"] == "application/pdf"
    assert result["size"] == 1024
    assert result["etag"] == "abc123"
    assert result["is_directory"] is False


@pytest.mark.unit
async def test_get_file_info_returns_none_for_missing_file(mocker):
    """Test that get_file_info returns None for missing files."""
    from httpx import HTTPStatusError, Response

    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock 404 response
    mock_response = mocker.Mock(spec=Response)
    mock_response.status_code = 404
    mock_http_client.request = AsyncMock(
        side_effect=HTTPStatusError(
            "Not Found", request=mocker.Mock(), response=mock_response
        )
    )

    # Call get_file_info
    result = await client.get_file_info("nonexistent.pdf")

    # Verify result is None
    assert result is None


@pytest.mark.unit
async def test_create_tag_creates_system_tag(mocker):
    """Test that create_tag creates a system tag via WebDAV."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock WebDAV response with Content-Location header
    mock_response = AsyncMock()
    mock_response.status_code = 201
    mock_response.headers = {"Content-Location": "/remote.php/dav/systemtags/42"}
    mock_response.raise_for_status = mocker.Mock()

    mock_http_client.post = AsyncMock(return_value=mock_response)

    # Call create_tag
    result = await client.create_tag("vector-index")

    # Verify result
    assert result["id"] == 42
    assert result["name"] == "vector-index"
    assert result["userVisible"] is True
    assert result["userAssignable"] is True

    # Verify API call
    mock_http_client.post.assert_called_once()
    call_args = mock_http_client.post.call_args
    assert call_args[0][0] == "/remote.php/dav/systemtags/"
    assert call_args[1]["json"]["name"] == "vector-index"
    assert call_args[1]["json"]["userVisible"] is True
    assert call_args[1]["json"]["userAssignable"] is True


@pytest.mark.unit
async def test_get_or_create_tag_returns_existing_tag(mocker):
    """Test that get_or_create_tag returns existing tag without creating."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock existing tag
    mocker.patch.object(
        client,
        "get_tag_by_name",
        return_value={"id": 42, "name": "vector-index", "userVisible": True},
    )
    mock_create = mocker.patch.object(client, "create_tag")

    # Call get_or_create_tag
    result = await client.get_or_create_tag("vector-index")

    # Verify existing tag returned without creating
    assert result["id"] == 42
    mock_create.assert_not_called()


@pytest.mark.unit
async def test_get_or_create_tag_creates_new_tag(mocker):
    """Test that get_or_create_tag creates tag when not found."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock no existing tag
    mocker.patch.object(client, "get_tag_by_name", return_value=None)
    mocker.patch.object(
        client,
        "create_tag",
        return_value={"id": 42, "name": "vector-index", "userVisible": True},
    )

    # Call get_or_create_tag
    result = await client.get_or_create_tag("vector-index")

    # Verify tag was created
    assert result["id"] == 42
    client.create_tag.assert_called_once_with("vector-index", True, True)


@pytest.mark.unit
async def test_assign_tag_to_file_success(mocker):
    """Test that assign_tag_to_file assigns tag via WebDAV."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock 201 Created response
    mock_response = AsyncMock()
    mock_response.status_code = 201

    mock_http_client.request = AsyncMock(return_value=mock_response)

    # Call assign_tag_to_file
    result = await client.assign_tag_to_file(12345, 42)

    # Verify result
    assert result is True

    # Verify API call
    mock_http_client.request.assert_called_once()
    call_args = mock_http_client.request.call_args
    assert call_args[0][0] == "PUT"
    assert "/systemtags-relations/files/12345/42" in call_args[0][1]


@pytest.mark.unit
async def test_assign_tag_to_file_already_assigned(mocker):
    """Test that assign_tag_to_file handles already assigned (409) gracefully."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock 409 Conflict response (already assigned)
    mock_response = AsyncMock()
    mock_response.status_code = 409

    mock_http_client.request = AsyncMock(return_value=mock_response)

    # Call assign_tag_to_file
    result = await client.assign_tag_to_file(12345, 42)

    # Verify result (should succeed even with 409)
    assert result is True


@pytest.mark.unit
async def test_remove_tag_from_file_success(mocker):
    """Test that remove_tag_from_file removes tag via WebDAV."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock 204 No Content response
    mock_response = AsyncMock()
    mock_response.status_code = 204

    mock_http_client.request = AsyncMock(return_value=mock_response)

    # Call remove_tag_from_file
    result = await client.remove_tag_from_file(12345, 42)

    # Verify result
    assert result is True

    # Verify API call
    mock_http_client.request.assert_called_once()
    call_args = mock_http_client.request.call_args
    assert call_args[0][0] == "DELETE"
    assert "/systemtags-relations/files/12345/42" in call_args[0][1]


@pytest.mark.unit
async def test_remove_tag_from_file_not_assigned(mocker):
    """Test that remove_tag_from_file handles not assigned (404) gracefully."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Mock 404 Not Found response (tag wasn't assigned)
    mock_response = AsyncMock()
    mock_response.status_code = 404

    mock_http_client.request = AsyncMock(return_value=mock_response)

    # Call remove_tag_from_file
    result = await client.remove_tag_from_file(12345, 42)

    # Verify result (should succeed even with 404)
    assert result is True
