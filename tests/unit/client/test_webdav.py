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
