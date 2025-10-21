"""Integration tests for WebDAV search MCP tools."""

import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


def normalize_search_response(data):
    """Extract results list from SearchFilesResponse.

    The response is a SearchFilesResponse with a 'results' field containing the list of files.
    """
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    else:
        # Fallback for unexpected format
        return []


@pytest.fixture
async def search_test_files(nc_client: NextcloudClient):
    """Create test files for WebDAV search testing via MCP."""
    test_dir = f"mcp_webdav_search_{uuid.uuid4().hex[:8]}"

    # Create base directory
    await nc_client.webdav.create_directory(test_dir)

    # Create various test files
    test_files = [
        # Text files
        (f"{test_dir}/search_test1.txt", b"Sample document", "text/plain"),
        (f"{test_dir}/search_test2.txt", b"Another document", "text/plain"),
        (f"{test_dir}/search_report.txt", b"Report content", "text/plain"),
        # Markdown files
        (f"{test_dir}/search_readme.md", b"# README", "text/markdown"),
        (f"{test_dir}/search_notes.md", b"# Notes", "text/markdown"),
        # Images (simulated)
        (f"{test_dir}/search_image.jpg", b"\xff\xd8\xff fake jpg", "image/jpeg"),
        (f"{test_dir}/search_photo.png", b"\x89PNG fake png", "image/png"),
        # PDF (simulated)
        (f"{test_dir}/search_presentation.pdf", b"%PDF-1.4", "application/pdf"),
    ]

    # Write all test files
    for file_path, content, content_type in test_files:
        await nc_client.webdav.write_file(file_path, content, content_type)

    logger.info(f"Created {len(test_files)} test files in {test_dir}")

    yield test_dir

    # Cleanup
    try:
        await nc_client.webdav.delete_resource(test_dir)
        logger.info(f"Cleaned up test directory: {test_dir}")
    except Exception as e:
        logger.warning(f"Failed to cleanup {test_dir}: {e}")


async def test_nc_webdav_find_by_name(
    nc_mcp_client: ClientSession, search_test_files: str
):
    """Test nc_webdav_find_by_name MCP tool."""
    # Find all .txt files in the test directory
    result = await nc_mcp_client.call_tool(
        "nc_webdav_find_by_name",
        arguments={
            "pattern": "search_%.txt",
            "scope": search_test_files,
        },
    )

    # Parse the result
    content = result.content[0].text
    files = normalize_search_response(json.loads(content))

    logger.info(f"Found {len(files)} files matching 'search_%.txt'")

    # Should find at least 3 .txt files
    assert len(files) >= 3, f"Expected at least 3 .txt files, got {len(files)}"

    # Verify all results end with .txt
    for file in files:
        name = file.get("name", "")
        assert name.endswith(".txt"), f"Expected .txt file, got {name}"
        assert name.startswith("search_"), (
            f"Expected name to start with 'search_', got {name}"
        )


async def test_nc_webdav_find_by_name_with_limit(
    nc_mcp_client: ClientSession, search_test_files: str
):
    """Test nc_webdav_find_by_name with limit parameter."""
    # Find files with limit
    result = await nc_mcp_client.call_tool(
        "nc_webdav_find_by_name",
        arguments={
            "pattern": "search_%.txt",
            "scope": search_test_files,
            "limit": 2,
        },
    )

    content = result.content[0].text
    files = normalize_search_response(json.loads(content))

    logger.info(f"Found {len(files)} files with limit=2")

    # Should return at most 2 results
    assert len(files) <= 2, f"Expected at most 2 files, got {len(files)}"
    assert len(files) > 0, "Expected at least 1 file"


async def test_nc_webdav_find_by_type_images(
    nc_mcp_client: ClientSession, search_test_files: str
):
    """Test nc_webdav_find_by_type for images."""
    # Find all images
    result = await nc_mcp_client.call_tool(
        "nc_webdav_find_by_type",
        arguments={
            "mime_type": "image/%",
            "scope": search_test_files,
        },
    )

    content = result.content[0].text
    files = normalize_search_response(json.loads(content))

    logger.info(f"Found {len(files)} image files")

    # Should find at least 2 image files (jpg and png)
    assert len(files) >= 2, f"Expected at least 2 image files, got {len(files)}"

    # Verify all results are images
    for file in files:
        content_type = file.get("content_type", "")
        assert content_type.startswith("image/"), (
            f"Expected image/* type, got {content_type}"
        )


async def test_nc_webdav_find_by_type_specific(
    nc_mcp_client: ClientSession, search_test_files: str
):
    """Test nc_webdav_find_by_type for specific MIME type."""
    # Find PDF files
    result = await nc_mcp_client.call_tool(
        "nc_webdav_find_by_type",
        arguments={
            "mime_type": "application/pdf",
            "scope": search_test_files,
        },
    )

    content = result.content[0].text
    files = normalize_search_response(json.loads(content))

    logger.info(f"Found {len(files)} PDF files")

    # Should find at least 1 PDF
    assert len(files) >= 1, f"Expected at least 1 PDF file, got {len(files)}"

    # Verify result is PDF
    for file in files:
        content_type = file.get("content_type", "")
        assert content_type == "application/pdf", (
            f"Expected application/pdf, got {content_type}"
        )


async def test_nc_webdav_search_files_basic(
    nc_mcp_client: ClientSession, search_test_files: str
):
    """Test nc_webdav_search_files with basic filters."""
    # Search for markdown files
    result = await nc_mcp_client.call_tool(
        "nc_webdav_search_files",
        arguments={
            "scope": search_test_files,
            "name_pattern": "%.md",
        },
    )

    content = result.content[0].text
    files = normalize_search_response(json.loads(content))

    logger.info(f"Found {len(files)} markdown files")

    # Should find at least 2 .md files
    assert len(files) >= 2, f"Expected at least 2 .md files, got {len(files)}"

    # Verify all results are .md files
    for file in files:
        name = file.get("name", "")
        assert name.endswith(".md"), f"Expected .md file, got {name}"


async def test_nc_webdav_search_files_combined(
    nc_mcp_client: ClientSession, search_test_files: str
):
    """Test nc_webdav_search_files with combined filters."""
    # Search for text files with specific name pattern
    result = await nc_mcp_client.call_tool(
        "nc_webdav_search_files",
        arguments={
            "scope": search_test_files,
            "name_pattern": "search_test%.txt",
            "mime_type": "text/plain",
        },
    )

    content = result.content[0].text
    files = normalize_search_response(json.loads(content))

    logger.info(f"Found {len(files)} files matching combined filters")

    # Should find search_test1.txt and search_test2.txt
    assert len(files) >= 2, f"Expected at least 2 files, got {len(files)}"

    # Verify all results match both conditions
    for file in files:
        name = file.get("name", "")
        content_type = file.get("content_type", "")
        assert name.endswith(".txt"), f"Expected .txt file, got {name}"
        assert name.startswith("search_test"), (
            f"Expected 'search_test' prefix, got {name}"
        )
        assert content_type == "text/plain", f"Expected text/plain, got {content_type}"


async def test_nc_webdav_search_files_with_limit(
    nc_mcp_client: ClientSession, search_test_files: str
):
    """Test nc_webdav_search_files with result limit."""
    # Search with limit
    result = await nc_mcp_client.call_tool(
        "nc_webdav_search_files",
        arguments={
            "scope": search_test_files,
            "name_pattern": "search_%",
            "limit": 3,
        },
    )

    content = result.content[0].text
    files = normalize_search_response(json.loads(content))

    logger.info(f"Found {len(files)} files with limit=3")

    # Should return at most 3 results
    assert len(files) <= 3, f"Expected at most 3 files, got {len(files)}"
    assert len(files) > 0, "Expected at least 1 file"


async def test_nc_webdav_search_no_results(
    nc_mcp_client: ClientSession, search_test_files: str
):
    """Test search that returns no results."""
    # Search for non-existent pattern
    result = await nc_mcp_client.call_tool(
        "nc_webdav_find_by_name",
        arguments={
            "pattern": "nonexistent_xyz123.txt",
            "scope": search_test_files,
        },
    )

    # Handle case where empty results might return empty content
    if result.content and len(result.content) > 0:
        content = result.content[0].text
        files = normalize_search_response(json.loads(content))
    else:
        files = []

    logger.info("Search correctly returned no results")

    # Should return empty array
    assert len(files) == 0, f"Expected no results, got {len(files)}"


async def test_search_result_properties(
    nc_mcp_client: ClientSession, search_test_files: str
):
    """Test that search results include expected properties."""
    # Search for a specific file
    result = await nc_mcp_client.call_tool(
        "nc_webdav_find_by_name",
        arguments={
            "pattern": "search_readme.md",
            "scope": search_test_files,
        },
    )

    content = result.content[0].text
    files = normalize_search_response(json.loads(content))

    assert len(files) >= 1, "Should find at least one file"

    file = files[0]

    # Check for expected properties
    assert "name" in file, "Should include name property"
    assert "path" in file, "Should include path property"
    assert "is_directory" in file, "Should include is_directory property"
    assert file["is_directory"] is False, "File should not be a directory"

    # Check for extended properties from search
    extended_props = ["file_id", "etag", "size", "content_type", "last_modified"]
    present_props = [prop for prop in extended_props if prop in file]

    logger.info(f"Search result properties: {list(file.keys())}")
    assert len(present_props) > 0, f"Should have at least one of {extended_props}"
