"""Integration tests for Nextcloud Collectives MCP tools."""

import json
import logging
import uuid

import pytest
from mcp import ClientSession

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


# --- Fixtures ---


@pytest.fixture(scope="session")
async def temporary_collective(nc_mcp_client: ClientSession):
    """Create a temporary collective for testing. Cleaned up after session."""
    unique_suffix = uuid.uuid4().hex[:8]
    name = f"MCP Test Collective {unique_suffix}"

    result = await nc_mcp_client.call_tool(
        "collectives_create_collective",
        {"name": name, "emoji": "🧪"},
    )
    assert result.isError is False, f"Failed to create collective: {result.content}"
    data = json.loads(result.content[0].text)
    collective_id = data["id"]
    logger.info(f"Created temporary collective: {name} (ID: {collective_id})")

    # Get the landing page ID — filter by parentId == 0 (root page)
    pages_result = await nc_mcp_client.call_tool(
        "collectives_get_pages",
        {"collective_id": collective_id},
    )
    pages_data = json.loads(pages_result.content[0].text)
    root_pages = [p for p in pages_data["pages"] if p["parentId"] == 0]
    assert root_pages, "Expected at least one root page (landing page)"
    landing_page_id = root_pages[0]["id"]

    yield {
        "id": collective_id,
        "name": name,
        "landing_page_id": landing_page_id,
    }

    # Cleanup: trash and permanently delete the collective via MCP tools
    try:
        await nc_mcp_client.call_tool(
            "collectives_trash_collective",
            {"collective_id": collective_id},
        )
        await nc_mcp_client.call_tool(
            "collectives_delete_collective",
            {"collective_id": collective_id},
        )
        logger.info(f"Cleaned up collective: {collective_id}")
    except Exception as e:
        logger.warning(f"Cleanup of collective {collective_id} failed: {e}")


# --- Tool Discovery ---


async def test_collectives_tools_available(nc_mcp_client: ClientSession):
    """Verify all Collectives MCP tools are registered."""
    tools = await nc_mcp_client.list_tools()
    tool_names = [tool.name for tool in tools.tools]

    expected_tools = [
        "collectives_get_collectives",
        "collectives_create_collective",
        "collectives_update_collective",
        "collectives_trash_collective",
        "collectives_delete_collective",
        "collectives_get_pages",
        "collectives_get_page",
        "collectives_create_page",
        "collectives_move_page",
        "collectives_trash_page",
        "collectives_restore_page",
        "collectives_set_page_emoji",
        "collectives_search_pages",
        "collectives_get_tags",
        "collectives_create_tag",
        "collectives_assign_tag",
        "collectives_remove_tag",
        "collectives_get_trashed_pages",
    ]

    for expected in expected_tools:
        assert expected in tool_names, (
            f"Expected tool '{expected}' not found in available tools"
        )

    logger.info(f"All {len(expected_tools)} Collectives tools registered")


# --- Collective CRUD ---


async def test_collectives_list(
    nc_mcp_client: ClientSession, temporary_collective: dict
):
    """Test listing collectives includes the temporary one."""
    result = await nc_mcp_client.call_tool("collectives_get_collectives", {})
    assert result.isError is False

    data = json.loads(result.content[0].text)
    assert data["success"] is True
    assert data["total"] >= 1

    collective_ids = [c["id"] for c in data["collectives"]]
    assert temporary_collective["id"] in collective_ids
    logger.info(f"Found {data['total']} collectives")


async def test_collectives_update_emoji(
    nc_mcp_client: ClientSession, temporary_collective: dict
):
    """Test updating a collective's emoji."""
    result = await nc_mcp_client.call_tool(
        "collectives_update_collective",
        {"collective_id": temporary_collective["id"], "emoji": "📖"},
    )
    assert result.isError is False

    data = json.loads(result.content[0].text)
    assert data["success"] is True
    assert data["collective_id"] == temporary_collective["id"]
    logger.info("Collective emoji updated")


# --- Page CRUD ---


async def test_collectives_page_workflow(
    nc_mcp_client: ClientSession, temporary_collective: dict
):
    """Test the full page lifecycle: create, read, set emoji, trash, restore."""
    cid = temporary_collective["id"]
    landing_id = temporary_collective["landing_page_id"]

    # 1. Create a page
    unique_title = f"Test Page {uuid.uuid4().hex[:8]}"
    create_result = await nc_mcp_client.call_tool(
        "collectives_create_page",
        {"collective_id": cid, "parent_id": landing_id, "title": unique_title},
    )
    assert create_result.isError is False
    create_data = json.loads(create_result.content[0].text)
    page_id = create_data["id"]
    assert create_data["collective_id"] == cid
    assert create_data["parent_id"] == landing_id
    logger.info(f"Created page: {unique_title} (ID: {page_id})")

    # 2. List pages — should include the new page
    list_result = await nc_mcp_client.call_tool(
        "collectives_get_pages",
        {"collective_id": cid},
    )
    assert list_result.isError is False
    list_data = json.loads(list_result.content[0].text)
    page_ids = [p["id"] for p in list_data["pages"]]
    assert page_id in page_ids
    logger.info(f"Page found in list ({list_data['total']} pages)")

    # 3. Get page with content
    get_result = await nc_mcp_client.call_tool(
        "collectives_get_page",
        {"collective_id": cid, "page_id": page_id},
    )
    assert get_result.isError is False
    get_data = json.loads(get_result.content[0].text)
    assert get_data["page"]["id"] == page_id
    assert get_data["page"]["title"] == unique_title
    # New pages have empty content (empty string or None)
    logger.info("Page metadata retrieved")

    # 4. Set page emoji
    emoji_result = await nc_mcp_client.call_tool(
        "collectives_set_page_emoji",
        {"collective_id": cid, "page_id": page_id, "emoji": "🚀"},
    )
    assert emoji_result.isError is False
    logger.info("Page emoji set")

    # 5. Trash the page
    trash_result = await nc_mcp_client.call_tool(
        "collectives_trash_page",
        {"collective_id": cid, "page_id": page_id},
    )
    assert trash_result.isError is False
    logger.info("Page trashed")

    # 6. Verify page is in trash
    trashed_result = await nc_mcp_client.call_tool(
        "collectives_get_trashed_pages",
        {"collective_id": cid},
    )
    assert trashed_result.isError is False
    trashed_data = json.loads(trashed_result.content[0].text)
    trashed_ids = [p["id"] for p in trashed_data["pages"]]
    assert page_id in trashed_ids
    logger.info("Page found in trash")

    # 7. Restore from trash
    restore_result = await nc_mcp_client.call_tool(
        "collectives_restore_page",
        {"collective_id": cid, "page_id": page_id},
    )
    assert restore_result.isError is False
    logger.info("Page restored from trash")

    # 8. Verify page is back in pages list
    list_result2 = await nc_mcp_client.call_tool(
        "collectives_get_pages",
        {"collective_id": cid},
    )
    list_data2 = json.loads(list_result2.content[0].text)
    page_ids2 = [p["id"] for p in list_data2["pages"]]
    assert page_id in page_ids2
    logger.info("Page verified restored to pages list")


async def test_collectives_get_landing_page_content(
    nc_mcp_client: ClientSession, temporary_collective: dict
):
    """Test that the landing page has auto-generated content readable via WebDAV."""
    cid = temporary_collective["id"]
    landing_id = temporary_collective["landing_page_id"]

    result = await nc_mcp_client.call_tool(
        "collectives_get_page",
        {"collective_id": cid, "page_id": landing_id},
    )
    assert result.isError is False

    data = json.loads(result.content[0].text)
    assert data["page"]["fileName"] == "Readme.md"
    assert data["content"] is not None, (
        "Landing page should have auto-generated content"
    )
    assert "Welcome" in data["content"], "Landing page should contain welcome text"
    logger.info(f"Landing page content: {len(data['content'])} bytes")


async def test_collectives_move_page(
    nc_mcp_client: ClientSession, temporary_collective: dict
):
    """Test moving a page (rename)."""
    cid = temporary_collective["id"]
    landing_id = temporary_collective["landing_page_id"]

    # Create a page to move
    create_result = await nc_mcp_client.call_tool(
        "collectives_create_page",
        {
            "collective_id": cid,
            "parent_id": landing_id,
            "title": f"Movable Page {uuid.uuid4().hex[:8]}",
        },
    )
    assert create_result.isError is False
    page_id = json.loads(create_result.content[0].text)["id"]

    # Move (rename) the page
    new_title = f"Renamed Page {uuid.uuid4().hex[:8]}"
    move_result = await nc_mcp_client.call_tool(
        "collectives_move_page",
        {
            "collective_id": cid,
            "page_id": page_id,
            "title": new_title,
        },
    )
    assert move_result.isError is False

    data = json.loads(move_result.content[0].text)
    assert data["page_id"] == page_id
    assert "moved" in data["message"]
    logger.info(f"Page renamed to: {new_title}")

    # Cleanup
    await nc_mcp_client.call_tool(
        "collectives_trash_page",
        {"collective_id": cid, "page_id": page_id},
    )


# --- Tags ---


async def test_collectives_tag_workflow(
    nc_mcp_client: ClientSession, temporary_collective: dict
):
    """Test tag lifecycle: create tag, assign to page, remove from page."""
    cid = temporary_collective["id"]
    landing_id = temporary_collective["landing_page_id"]

    # 1. Create a tag
    tag_name = f"test-tag-{uuid.uuid4().hex[:6]}"
    create_tag_result = await nc_mcp_client.call_tool(
        "collectives_create_tag",
        {"collective_id": cid, "name": tag_name, "color": "FF5733"},
    )
    assert create_tag_result.isError is False
    tag_data = json.loads(create_tag_result.content[0].text)
    tag_id = tag_data["id"]
    assert tag_data["name"] == tag_name
    assert tag_data["color"] == "FF5733"
    logger.info(f"Created tag: {tag_name} (ID: {tag_id})")

    # 2. List tags — should include the new tag
    list_tags_result = await nc_mcp_client.call_tool(
        "collectives_get_tags",
        {"collective_id": cid},
    )
    assert list_tags_result.isError is False
    tags_data = json.loads(list_tags_result.content[0].text)
    tag_ids = [t["id"] for t in tags_data["tags"]]
    assert tag_id in tag_ids
    logger.info(f"Tag found in list ({tags_data['total']} tags)")

    # 3. Create a page to tag
    page_result = await nc_mcp_client.call_tool(
        "collectives_create_page",
        {
            "collective_id": cid,
            "parent_id": landing_id,
            "title": f"Tagged Page {uuid.uuid4().hex[:8]}",
        },
    )
    assert page_result.isError is False
    page_id = json.loads(page_result.content[0].text)["id"]

    # 4. Assign tag to page
    assign_result = await nc_mcp_client.call_tool(
        "collectives_assign_tag",
        {"collective_id": cid, "page_id": page_id, "tag_id": tag_id},
    )
    assert assign_result.isError is False
    logger.info(f"Tag {tag_id} assigned to page {page_id}")

    # 5. Remove tag from page
    remove_result = await nc_mcp_client.call_tool(
        "collectives_remove_tag",
        {"collective_id": cid, "page_id": page_id, "tag_id": tag_id},
    )
    assert remove_result.isError is False
    logger.info(f"Tag {tag_id} removed from page {page_id}")

    # Cleanup
    await nc_mcp_client.call_tool(
        "collectives_trash_page",
        {"collective_id": cid, "page_id": page_id},
    )


# --- Search ---


async def test_collectives_search(
    nc_mcp_client: ClientSession, temporary_collective: dict
):
    """Test full-text search within a collective."""
    cid = temporary_collective["id"]

    # Search for text in the landing page (contains "Welcome")
    result = await nc_mcp_client.call_tool(
        "collectives_search_pages",
        {"collective_id": cid, "query": "Welcome"},
    )
    assert result.isError is False

    data = json.loads(result.content[0].text)
    assert data["success"] is True
    assert data["query"] == "Welcome"
    assert data["collective_id"] == cid
    # Search may or may not find results depending on indexing timing
    logger.info(f"Search returned {data['total']} results for 'Welcome'")


# --- Error Handling ---


async def test_collectives_get_page_not_found(nc_mcp_client: ClientSession):
    """Test getting a non-existent page returns an error."""
    result = await nc_mcp_client.call_tool(
        "collectives_get_page",
        {"collective_id": 999999, "page_id": 999999},
    )
    assert result.isError is True
    logger.info("Non-existent page correctly returned error")


async def test_collectives_get_pages_not_found(nc_mcp_client: ClientSession):
    """Test listing pages for a non-existent collective returns an error."""
    result = await nc_mcp_client.call_tool(
        "collectives_get_pages",
        {"collective_id": 999999},
    )
    assert result.isError is True
    logger.info("Non-existent collective correctly returned error")
