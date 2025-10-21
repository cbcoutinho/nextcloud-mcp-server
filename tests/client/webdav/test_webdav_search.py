"""Integration tests for WebDAV search operations."""

import logging
import uuid

import pytest

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture
async def test_search_setup(nc_client: NextcloudClient):
    """Create test files and directories for search testing."""
    test_dir = f"mcp_search_test_{uuid.uuid4().hex[:8]}"

    # Create base directory
    await nc_client.webdav.create_directory(test_dir)

    # Create various test files
    test_files = [
        # Text files
        (f"{test_dir}/document1.txt", b"Sample document content", "text/plain"),
        (f"{test_dir}/document2.txt", b"Another document", "text/plain"),
        (f"{test_dir}/report.txt", b"Report content", "text/plain"),
        # Markdown files
        (f"{test_dir}/readme.md", b"# README\nMarkdown content", "text/markdown"),
        (f"{test_dir}/notes.md", b"# Notes\nSome notes here", "text/markdown"),
        # PDF (simulated as binary)
        (
            f"{test_dir}/presentation.pdf",
            b"%PDF-1.4 fake pdf content",
            "application/pdf",
        ),
        # Subdirectory with files
        (f"{test_dir}/subdir/nested.txt", b"Nested file content", "text/plain"),
    ]

    # Create subdirectory
    await nc_client.webdav.create_directory(f"{test_dir}/subdir")

    # Write all test files
    for file_path, content, content_type in test_files:
        await nc_client.webdav.write_file(file_path, content, content_type)

    logger.info(f"Created test directory with {len(test_files)} files: {test_dir}")

    yield test_dir

    # Cleanup
    try:
        await nc_client.webdav.delete_resource(test_dir)
        logger.info(f"Cleaned up test directory: {test_dir}")
    except Exception as e:
        logger.warning(f"Failed to cleanup test directory {test_dir}: {e}")


async def test_find_by_name_exact(nc_client: NextcloudClient, test_search_setup: str):
    """Test finding files by exact name."""
    results = await nc_client.webdav.find_by_name("readme.md", scope=test_search_setup)

    assert len(results) >= 1, "Should find at least one readme.md file"

    # Check that we found the right file
    readme_files = [r for r in results if r.get("name") == "readme.md"]
    assert len(readme_files) >= 1, "Should find readme.md"

    logger.info(f"Found {len(results)} files matching 'readme.md'")


async def test_find_by_name_wildcard_extension(
    nc_client: NextcloudClient, test_search_setup: str
):
    """Test finding files by extension using wildcard."""
    # Find all .txt files
    results = await nc_client.webdav.find_by_name("%.txt", scope=test_search_setup)

    assert len(results) >= 3, "Should find at least 3 .txt files"

    # Verify all results are .txt files
    for result in results:
        name = result.get("name", "")
        assert name.endswith(".txt"), f"Expected .txt file, got {name}"

    logger.info(f"Found {len(results)} .txt files")


async def test_find_by_name_wildcard_prefix(
    nc_client: NextcloudClient, test_search_setup: str
):
    """Test finding files by name prefix using wildcard."""
    # Find all files starting with "document"
    results = await nc_client.webdav.find_by_name("document%", scope=test_search_setup)

    assert len(results) >= 2, "Should find at least 2 files starting with 'document'"

    # Verify all results start with "document"
    for result in results:
        name = result.get("name", "")
        assert name.startswith("document"), (
            f"Expected name to start with 'document', got {name}"
        )

    logger.info(f"Found {len(results)} files starting with 'document'")


async def test_find_by_type_text(nc_client: NextcloudClient, test_search_setup: str):
    """Test finding files by MIME type (text files)."""
    # Find all text files
    results = await nc_client.webdav.find_by_type("text/%", scope=test_search_setup)

    assert len(results) >= 5, "Should find at least 5 text files"

    # Verify all results are text files
    for result in results:
        content_type = result.get("content_type", "")
        assert content_type.startswith("text/"), (
            f"Expected text/* type, got {content_type}"
        )

    logger.info(f"Found {len(results)} text files")


async def test_find_by_type_specific(
    nc_client: NextcloudClient, test_search_setup: str
):
    """Test finding files by specific MIME type."""
    # Find PDF files
    results = await nc_client.webdav.find_by_type(
        "application/pdf", scope=test_search_setup
    )

    assert len(results) >= 1, "Should find at least 1 PDF file"

    # Verify result is PDF
    for result in results:
        content_type = result.get("content_type", "")
        assert content_type == "application/pdf", (
            f"Expected application/pdf, got {content_type}"
        )

    logger.info(f"Found {len(results)} PDF files")


async def test_search_with_limit(nc_client: NextcloudClient, test_search_setup: str):
    """Test search with result limit."""
    # Search for .txt files with limit of 2
    results = await nc_client.webdav.find_by_name(
        "%.txt", scope=test_search_setup, limit=2
    )

    # Should return at most 2 results
    assert len(results) <= 2, f"Should return at most 2 results, got {len(results)}"
    assert len(results) > 0, "Should return at least 1 result"

    logger.info(f"Found {len(results)} files with limit=2")


async def test_search_files_combined_filters(
    nc_client: NextcloudClient, test_search_setup: str
):
    """Test search with multiple filters combined."""
    # This test uses the search_files method directly to test combined conditions
    # Search for .txt files that match a specific pattern
    where_conditions = """
        <d:and>
            <d:like>
                <d:prop>
                    <d:displayname/>
                </d:prop>
                <d:literal>%.txt</d:literal>
            </d:like>
            <d:like>
                <d:prop>
                    <d:displayname/>
                </d:prop>
                <d:literal>document%</d:literal>
            </d:like>
        </d:and>
    """

    results = await nc_client.webdav.search_files(
        scope=test_search_setup, where_conditions=where_conditions
    )

    # Should find document1.txt and document2.txt
    assert len(results) >= 2, "Should find at least 2 files matching both conditions"

    # Verify results match both conditions
    for result in results:
        name = result.get("name", "")
        assert name.endswith(".txt"), f"Expected .txt file, got {name}"
        assert name.startswith("document"), (
            f"Expected name to start with 'document', got {name}"
        )

    logger.info(f"Found {len(results)} files matching combined filters")


async def test_search_empty_scope(nc_client: NextcloudClient, test_search_setup: str):
    """Test search in empty scope (user root)."""
    # Search entire user root for a unique filename
    unique_name = "readme.md"
    results = await nc_client.webdav.find_by_name(unique_name, scope="")

    # Should find at least the one we created
    assert len(results) >= 1, f"Should find at least 1 file named {unique_name}"

    logger.info(f"Found {len(results)} files in root scope")


async def test_search_subdirectory(nc_client: NextcloudClient, test_search_setup: str):
    """Test search within a subdirectory."""
    # Search in the subdir for the nested file
    results = await nc_client.webdav.find_by_name(
        "nested.txt", scope=f"{test_search_setup}/subdir"
    )

    assert len(results) >= 1, "Should find nested.txt in subdirectory"

    # Verify the file path
    nested_file = results[0]
    assert "nested.txt" in nested_file.get("name", ""), "Should find nested.txt"

    logger.info(f"Found file in subdirectory: {nested_file.get('name')}")


async def test_search_no_results(nc_client: NextcloudClient, test_search_setup: str):
    """Test search that returns no results."""
    # Search for a non-existent pattern
    results = await nc_client.webdav.find_by_name(
        "nonexistent_file_xyz123.txt", scope=test_search_setup
    )

    assert len(results) == 0, "Should return empty results for non-existent file"

    logger.info("Search correctly returned no results for non-existent file")


async def test_search_properties_returned(
    nc_client: NextcloudClient, test_search_setup: str
):
    """Test that search returns expected properties."""
    results = await nc_client.webdav.find_by_name("readme.md", scope=test_search_setup)

    assert len(results) >= 1, "Should find at least one file"

    result = results[0]

    # Check for expected properties
    assert "name" in result, "Should include name property"
    assert "path" in result, "Should include path property"
    assert "is_directory" in result, "Should include is_directory property"
    assert result["is_directory"] is False, "readme.md should not be a directory"

    # Optional properties that may be present
    optional_props = ["size", "content_type", "last_modified", "etag"]
    logger.info(f"Result properties: {list(result.keys())}")

    # At least some optional properties should be present
    present_optional = [prop for prop in optional_props if prop in result]
    assert len(present_optional) > 0, f"Should have at least one of {optional_props}"

    logger.info(f"Search returned properties: {list(result.keys())}")
