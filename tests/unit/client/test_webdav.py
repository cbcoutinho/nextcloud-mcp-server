"""Unit tests for WebDAV client."""

import xml.etree.ElementTree as ET
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
async def test_get_file_info_raises_on_404(mocker):
    """get_file_info now raises HTTPStatusError on 404 (was: returned None).

    The contract was widened so verify-on-read can distinguish a definitive
    404 from an ambiguous malformed-PROPFIND response. Callers that want
    "absent → None" semantics should catch HTTPStatusError and check the
    status code themselves.
    """
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

    with pytest.raises(HTTPStatusError) as exc_info:
        await client.get_file_info("nonexistent.pdf")

    assert exc_info.value.response.status_code == 404


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
    mock_create_tag = mocker.patch.object(
        client,
        "create_tag",
        return_value={"id": 42, "name": "vector-index", "userVisible": True},
    )

    # Call get_or_create_tag
    result = await client.get_or_create_tag("vector-index")

    # Verify tag was created
    assert result["id"] == 42
    mock_create_tag.assert_called_once_with("vector-index", True, True)


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


@pytest.mark.unit
async def test_get_files_by_tag_detects_directories(mocker):
    """get_files_by_tag must flag tagged folders via <d:resourcetype/>.

    Tagged folders need ``is_directory=True`` so the tag-exclusion layer
    (issue #710) can hide their descendants.
    """
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # Two-entry response: one regular file, one collection (folder).
    xml_content = b"""<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
        <d:response>
            <d:href>/remote.php/dav/files/testuser/Secret.txt</d:href>
            <d:propstat>
                <d:prop>
                    <oc:fileid>101</oc:fileid>
                    <d:displayname>Secret.txt</d:displayname>
                    <d:getcontentlength>42</d:getcontentlength>
                    <d:getcontenttype>text/plain</d:getcontenttype>
                    <d:getlastmodified>Wed, 01 Jan 2025 00:00:00 GMT</d:getlastmodified>
                    <d:getetag>"abc"</d:getetag>
                    <d:resourcetype/>
                </d:prop>
            </d:propstat>
        </d:response>
        <d:response>
            <d:href>/remote.php/dav/files/testuser/Private/</d:href>
            <d:propstat>
                <d:prop>
                    <oc:fileid>102</oc:fileid>
                    <d:displayname>Private</d:displayname>
                    <d:getlastmodified>Wed, 01 Jan 2025 00:00:00 GMT</d:getlastmodified>
                    <d:getetag>"def"</d:getetag>
                    <d:resourcetype><d:collection/></d:resourcetype>
                </d:prop>
            </d:propstat>
        </d:response>
    </d:multistatus>"""

    mock_response = AsyncMock()
    mock_response.content = xml_content
    mock_response.raise_for_status = mocker.Mock()
    mock_http_client.request = AsyncMock(return_value=mock_response)

    files = await client.get_files_by_tag(42)

    assert len(files) == 2
    by_path = {f["path"]: f for f in files}

    assert by_path["/Secret.txt"]["is_directory"] is False
    assert by_path["/Private/"]["is_directory"] is True

    # Sanity-check the REPORT body asks for resourcetype.
    call_args = mock_http_client.request.call_args
    assert "<d:resourcetype/>" in call_args.kwargs["content"]
    assert "<oc:systemtag>42</oc:systemtag>" in call_args.kwargs["content"]


@pytest.mark.unit
async def test_list_directory_decodes_non_ascii_names(mocker):
    """list_directory must percent-decode <d:href> for non-ASCII filenames (issue #776).

    RFC 3986 requires <d:href> to be percent-encoded, so a Chinese-named directory
    arrives as e.g. "%e5%ad%a6%e7%94%9f%e9%82%ae%e7%ae%b1". The MCP response should
    expose the decoded "学生邮箱", not the encoded form.
    """
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    # PROPFIND response with one Chinese-named subdirectory and one ASCII file.
    # The first <d:response> is the parent directory and is skipped by list_directory.
    xml_content = b"""<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:">
        <d:response>
            <d:href>/remote.php/dav/files/testuser/</d:href>
            <d:propstat>
                <d:prop>
                    <d:resourcetype><d:collection/></d:resourcetype>
                </d:prop>
            </d:propstat>
        </d:response>
        <d:response>
            <d:href>/remote.php/dav/files/testuser/%e5%ad%a6%e7%94%9f%e9%82%ae%e7%ae%b1/</d:href>
            <d:propstat>
                <d:prop>
                    <d:displayname>\xe5\xad\xa6\xe7\x94\x9f\xe9\x82\xae\xe7\xae\xb1</d:displayname>
                    <d:resourcetype><d:collection/></d:resourcetype>
                </d:prop>
            </d:propstat>
        </d:response>
        <d:response>
            <d:href>/remote.php/dav/files/testuser/notes.txt</d:href>
            <d:propstat>
                <d:prop>
                    <d:displayname>notes.txt</d:displayname>
                    <d:getcontentlength>10</d:getcontentlength>
                    <d:getcontenttype>text/plain</d:getcontenttype>
                    <d:resourcetype/>
                </d:prop>
            </d:propstat>
        </d:response>
    </d:multistatus>"""

    mock_response = AsyncMock()
    mock_response.content = xml_content
    mock_response.raise_for_status = mocker.Mock()
    mock_http_client.request = AsyncMock(return_value=mock_response)

    items = await client.list_directory("")

    by_name = {item["name"]: item for item in items}
    assert "学生邮箱" in by_name, f"expected decoded Chinese name, got: {list(by_name)}"
    assert by_name["学生邮箱"]["is_directory"] is True
    assert by_name["学生邮箱"]["path"] == "学生邮箱"

    # ASCII entries must keep working.
    assert "notes.txt" in by_name
    assert by_name["notes.txt"]["is_directory"] is False


@pytest.mark.unit
def test_parse_search_response_decodes_non_ascii_paths(mocker):
    """_parse_search_response must percent-decode <d:href> for non-ASCII paths (issue #776).

    Affects find_by_name, find_by_type, list_favorites, and search_files: the `path`
    and `href` fields would otherwise leak percent-encoded URL form to callers.
    """
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    xml_content = b"""<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
        <d:response>
            <d:href>/remote.php/dav/files/testuser/%e5%ad%a6%e7%94%9f%e9%82%ae%e7%ae%b1/report.pdf</d:href>
            <d:propstat>
                <d:prop>
                    <d:displayname>report.pdf</d:displayname>
                    <d:getcontenttype>application/pdf</d:getcontenttype>
                    <d:getcontentlength>1024</d:getcontentlength>
                    <d:resourcetype/>
                </d:prop>
            </d:propstat>
        </d:response>
    </d:multistatus>"""

    results = client._parse_search_response(xml_content, scope="")

    assert len(results) == 1
    assert results[0]["path"] == "学生邮箱/report.pdf"
    assert results[0]["href"] == "/remote.php/dav/files/testuser/学生邮箱/report.pdf"
    # name comes from <d:displayname>, which is not URL-encoded; sanity-check it.
    assert results[0]["name"] == "report.pdf"


def _request_url(mock_http_client) -> str:
    """Positional URL passed to the underlying httpx ``request`` call."""
    return mock_http_client.request.call_args[0][1]


@pytest.mark.unit
@pytest.mark.parametrize(
    "path, expected",
    [
        ("", "/remote.php/dav/files/testuser/"),
        ("/Documents/notes.txt", "/remote.php/dav/files/testuser/Documents/notes.txt"),
        ("Documents/notes.txt", "/remote.php/dav/files/testuser/Documents/notes.txt"),
        ("a/b #1.pdf", "/remote.php/dav/files/testuser/a/b%20%231.pdf"),
        ("law/x, y  z.pdf", "/remote.php/dav/files/testuser/law/x%2C%20y%20%20z.pdf"),
        (
            "学生邮箱/r.pdf",
            "/remote.php/dav/files/testuser/%E5%AD%A6%E7%94%9F%E9%82%AE%E7%AE%B1/r.pdf",
        ),
    ],
)
def test_webdav_path_encoding(path, expected):
    """_webdav_path encodes the decoded caller path once, preserving '/', and
    strips a leading slash. Every caller-path builder routes through this, so
    it is the single source of truth for their encoding."""
    client = WebDAVClient(AsyncMock(), "testuser")
    assert client._webdav_path(path) == expected


@pytest.mark.unit
def test_encode_dav_path_encodes_exactly_once():
    """Pins the decoded-input precondition: a literal '%' becomes '%25', so an
    already-encoded path passed in error would double-encode (caught here)."""
    from nextcloud_mcp_server.client.webdav import _encode_dav_path

    assert _encode_dav_path("already%20encoded.pdf") == "already%2520encoded.pdf"


@pytest.mark.unit
async def test_read_file_encodes_special_chars(mocker):
    """read_file must percent-encode '#', commas, and spaces in the path (card 309).

    Paths arrive already URL-decoded from PROPFIND/REPORT, so an unencoded '#'
    reaches httpx as a URL fragment and silently truncates the request → 404 on
    valid files (e.g. OHR-Bench law filenames). The outgoing request path must be
    percent-encoded.
    """
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")
    client._principal_discovered = True

    mock_response = AsyncMock()
    mock_response.content = b"%PDF-1.4 data"
    mock_response.headers = {"content-type": "application/pdf"}
    mock_response.raise_for_status = mocker.Mock()
    mock_http_client.request = AsyncMock(return_value=mock_response)

    # Name with a '#', a comma, a double space and a trailing space before ".pdf".
    await client.read_file("law/ADMA BioManufacturing, LLC -  Amendment #2 .pdf")

    url = _request_url(mock_http_client)
    assert url.startswith("/remote.php/dav/files/testuser/")
    # The hazardous characters are encoded; path separators are preserved.
    assert "%23" in url  # '#'
    assert "%2C" in url  # ','
    assert "%20" in url  # space
    assert "#" not in url
    assert ", " not in url
    assert "/law/" in url


@pytest.mark.unit
async def test_read_file_ascii_path_unchanged(mocker):
    """A plain ASCII path must pass through unchanged (no spurious encoding)."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")
    client._principal_discovered = True

    mock_response = AsyncMock()
    mock_response.content = b"data"
    mock_response.headers = {"content-type": "text/plain"}
    mock_response.raise_for_status = mocker.Mock()
    mock_http_client.request = AsyncMock(return_value=mock_response)

    await client.read_file("Documents/notes.txt")

    assert (
        _request_url(mock_http_client)
        == "/remote.php/dav/files/testuser/Documents/notes.txt"
    )


@pytest.mark.unit
async def test_read_file_raises_on_truncated_body(mocker):
    """A body shorter than Content-Length is a poisoned/truncated download (#965).

    httpx can hand back empty/short bytes on a healthy-looking 200 when a pooled
    keep-alive connection is desynced. read_file must raise a retryable transport
    error (so the vector-sync processor retries/re-queues instead of parsing the
    empty bytes and dead-lettering the file), not return the truncated content.
    """
    from httpx import RemoteProtocolError

    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    mock_response = AsyncMock()
    mock_response.content = b""  # poisoned connection delivered nothing
    mock_response.headers = {
        "content-type": "application/pdf",
        "content-length": "187564",
    }
    mock_response.raise_for_status = mocker.Mock()
    mock_http_client.request = AsyncMock(return_value=mock_response)

    with pytest.raises(RemoteProtocolError, match="Truncated download"):
        await client.read_file("Active-Personal/fw9-filled.pdf")


@pytest.mark.unit
async def test_read_file_accepts_matching_content_length(mocker):
    """A body matching Content-Length is returned unchanged."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    body = b"%PDF-1.7 full body"
    mock_response = AsyncMock()
    mock_response.content = body
    mock_response.headers = {
        "content-type": "application/pdf",
        "content-length": str(len(body)),
    }
    mock_response.raise_for_status = mocker.Mock()
    mock_http_client.request = AsyncMock(return_value=mock_response)

    content, content_type = await client.read_file("Documents/report.pdf")
    assert content == body
    assert content_type == "application/pdf"


@pytest.mark.unit
async def test_read_file_skips_check_without_content_length(mocker):
    """A header-less (e.g. chunked) response must not raise — nothing to compare."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    body = b"chunked body of unknown declared length"
    mock_response = AsyncMock()
    mock_response.content = body
    mock_response.headers = {"content-type": "text/plain"}  # no content-length
    mock_response.raise_for_status = mocker.Mock()
    mock_http_client.request = AsyncMock(return_value=mock_response)

    content, _ = await client.read_file("Documents/stream.txt")
    assert content == body


@pytest.mark.unit
async def test_get_note_attachment_raises_on_truncated_body(mocker):
    """get_note_attachment shares the short-read guard with read_file (#965)."""
    from httpx import RemoteProtocolError

    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")

    mock_response = AsyncMock()
    mock_response.content = b"abc"  # 3 bytes
    mock_response.headers = {
        "content-type": "application/pdf",
        "content-length": "2048",
    }
    mock_response.raise_for_status = mocker.Mock()
    mock_http_client.request = AsyncMock(return_value=mock_response)

    with pytest.raises(RemoteProtocolError, match="Truncated download"):
        await client.get_note_attachment(123, "doc.pdf")


@pytest.mark.unit
async def test_move_resource_encodes_destination_header(mocker):
    """The MOVE Destination header must be percent-encoded too (card 309)."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")
    client._principal_discovered = True

    mock_response = AsyncMock()
    mock_response.status_code = 201
    mock_response.raise_for_status = mocker.Mock()
    mock_http_client.request = AsyncMock(return_value=mock_response)

    await client.move_resource("a/old.pdf", "b/new #1.pdf")

    call = mock_http_client.request.call_args
    # Source is the request path; destination is the header.
    assert call[0][1] == "/remote.php/dav/files/testuser/a/old.pdf"
    destination = call.kwargs["headers"]["Destination"]
    assert "%23" in destination
    assert "#" not in destination


@pytest.mark.unit
async def test_copy_resource_encodes_destination_header(mocker):
    """The COPY Destination header must be percent-encoded too (card 309)."""
    mock_http_client = AsyncMock()
    client = WebDAVClient(mock_http_client, "testuser")
    client._principal_discovered = True

    mock_response = AsyncMock()
    mock_response.status_code = 201
    mock_response.raise_for_status = mocker.Mock()
    mock_http_client.request = AsyncMock(return_value=mock_response)

    await client.copy_resource("a/old.pdf", "b/new #1.pdf")

    call = mock_http_client.request.call_args
    assert call[0][1] == "/remote.php/dav/files/testuser/a/old.pdf"
    destination = call.kwargs["headers"]["Destination"]
    assert "%23" in destination
    assert "#" not in destination


@pytest.mark.unit
def test_build_search_xml_escapes_special_chars_in_scope():
    """A scope folder containing XML-special characters must yield a *well-formed*
    SEARCH body, not malformed XML that Nextcloud rejects with 400.

    Regression: a tagged folder whose name contains ``&`` (e.g. "Reports &
    Plans") injected a bare ``&`` into ``<d:href>``, so Nextcloud's Sabre/DAV
    parser 400'd the SEARCH and the tag-based indexing walk skipped the folder
    and *all* its descendants (silent indexing gap).
    """
    client = WebDAVClient(AsyncMock(), "testuser")
    scope = "Reports & Plans/2024"

    body = client._build_search_xml(
        scope=scope,
        where_conditions=None,
        properties=["displayname"],
        order_by=None,
        limit=None,
    )

    # 1. Must parse as well-formed XML (raised ParseError on the bare '&' before
    #    the fix).
    root = ET.fromstring(body)

    # 2. The href round-trips to the *literal* path — Sabre unescapes ``&amp;``
    #    back to ``&``, so matching is unchanged for folders without specials.
    href = root.find(".//{DAV:}href")
    assert href is not None
    assert href.text == f"/files/testuser/{scope}"

    # 3. The serialized body escapes the ampersand rather than emitting it raw.
    assert "& Plans" not in body  # no bare ampersand
    assert "&amp; Plans" in body  # escaped form present


@pytest.mark.unit
def test_build_search_xml_escapes_angle_brackets_in_scope():
    """``<`` / ``>`` in a folder name are escaped too (not just ``&``)."""
    client = WebDAVClient(AsyncMock(), "testuser")
    scope = "weird <name>"

    body = client._build_search_xml(
        scope=scope,
        where_conditions=None,
        properties=["displayname"],
        order_by=None,
        limit=None,
    )

    root = ET.fromstring(body)  # well-formed
    href = root.find(".//{DAV:}href")
    assert href is not None
    assert href.text == f"/files/testuser/{scope}"
    # Escaped forms present in the serialized body; raw angle brackets absent.
    assert "weird <name>" not in body
    assert "&lt;name&gt;" in body


@pytest.mark.unit
async def test_find_by_name_escapes_special_chars_in_pattern(mocker):
    # The filename pattern is embedded in a <d:literal>; '&'/'<'/'>' must be
    # escaped or the SEARCH body is malformed and Sabre 400s — the same bug class
    # as the scope fix. Regression for the find_by_name path.
    client = WebDAVClient(AsyncMock(), "testuser")
    mock_search = mocker.patch.object(client, "search_files", return_value=[])

    await client.find_by_name("Costs & Revenue <draft>.pdf")

    where = mock_search.call_args.kwargs["where_conditions"]
    # Well-formed once wrapped with the DAV namespace (raised ParseError pre-fix).
    ET.fromstring(f"<root xmlns:d='DAV:'>{where}</root>")
    assert "&amp;" in where and "&lt;draft&gt;" in where
    assert "Costs & Revenue" not in where  # no bare ampersand


@pytest.mark.unit
async def test_find_by_tag_escapes_special_chars_in_tag(mocker):
    # Tag names can contain '&' (e.g. "R&D"); the tag literal must be escaped too.
    client = WebDAVClient(AsyncMock(), "testuser")
    mock_search = mocker.patch.object(client, "search_files", return_value=[])

    await client.find_by_tag("R&D")

    where = mock_search.call_args.kwargs["where_conditions"]
    ET.fromstring(
        f"<root xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns'>{where}</root>"
    )
    assert "R&amp;D" in where
    assert "R&D" not in where  # no bare ampersand
