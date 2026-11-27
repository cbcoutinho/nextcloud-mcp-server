"""Integration tests for WebDAV search operations."""

import logging
import uuid

import pytest
from nextcloud_client import NextcloudClient

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
async def test_search_setup(anyio_backend, nc_client: NextcloudClient):
    """Create test files and directories for search testing. Module-scoped: every test only reads them."""
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

    logger.info("Created test directory with %s files: %s", len(test_files), test_dir)

    yield test_dir

    # Cleanup
    try:
        await nc_client.webdav.delete_resource(test_dir)
        logger.info("Cleaned up test directory: %s", test_dir)
    except Exception as e:
        logger.warning("Failed to cleanup test directory %s: %s", test_dir, e)


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

    logger.info("Found %s .txt files", len(results))


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

    logger.info("Found %s files starting with 'document'", len(results))


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

    logger.info("Found %s text files", len(results))


async def test_search_empty_scope(nc_client: NextcloudClient, test_search_setup: str):
    """Test search in empty scope (user root)."""
    # Search entire user root for a unique filename
    unique_name = "readme.md"
    results = await nc_client.webdav.find_by_name(unique_name, scope="")

    # Should find at least the one we created
    assert len(results) >= 1, f"Should find at least 1 file named {unique_name}"

    logger.info("Found %s files in root scope", len(results))


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

    logger.info("Found file in subdirectory: %s", nested_file.get("name"))


async def test_search_scope_with_ampersand_does_not_400(nc_client: NextcloudClient):
    """Regression: a folder whose name contains ``&`` must stay searchable.

    Before the scope was XML-escaped, the SEARCH body embedded a bare ``&`` in
    ``<d:href>``, so Nextcloud's Sabre/DAV parser rejected it with 400 and the
    tag-based indexing walk skipped the folder and every descendant (a silent
    indexing gap for any tenant with such a folder).
    """
    test_dir = f"mcp_amp_test_{uuid.uuid4().hex[:8]} & co"
    await nc_client.webdav.create_directory(test_dir)
    try:
        await nc_client.webdav.write_file(f"{test_dir}/doc.txt", b"hello", "text/plain")
        # Must not raise HTTP 400, and must locate the file within the '&' scope.
        results = await nc_client.webdav.find_by_name("doc.txt", scope=test_dir)
        assert any(r.get("name") == "doc.txt" for r in results), (
            f"file in '&'-named folder was not found via SEARCH: {results}"
        )
    finally:
        try:
            await nc_client.webdav.delete_resource(test_dir)
        except Exception as e:
            logger.warning("Failed to cleanup test directory %s: %s", test_dir, e)


async def test_search_with_xml_special_chars_in_pattern(nc_client: NextcloudClient):
    """A name pattern containing ``&`` must search, not 400.

    Regression for the unescaped ``<d:literal>``: an unescaped ``&`` makes the
    SEARCH body malformed, and Sabre/DAV rejects the whole request. The failure
    mode is what makes it worth an integration test -- the search errors rather
    than returning no matches, so a caller cannot tell "nothing matched" from
    "your query was rejected".
    """
    test_dir = f"mcp_search_amp_{uuid.uuid4().hex[:8]}"
    await nc_client.webdav.create_directory(test_dir)
    try:
        await nc_client.webdav.write_file(
            f"{test_dir}/Costs & Revenue.txt", b"quarterly", "text/plain"
        )

        results = await nc_client.webdav.find_by_name(
            "%Costs & Revenue%", scope=test_dir
        )

        assert [r["name"] for r in results] == ["Costs & Revenue.txt"]
    finally:
        await nc_client.webdav.delete_resource(test_dir)


async def test_search_with_xml_special_chars_in_mime_type(nc_client: NextcloudClient):
    """The mime_type predicate is escaped too, on the same code path.

    No real MIME type contains ``&``, so this asserts the request is *accepted*
    and simply matches nothing -- the behaviour an unescaped literal replaces
    with a 400.
    """
    results = await nc_client.webdav.find_by_type("text/<nonexistent & odd>")

    assert results == []
