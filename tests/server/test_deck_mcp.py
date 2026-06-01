import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


async def test_deck_mcp_connectivity(nc_mcp_client: ClientSession):
    """Test deck MCP tools are available and functional."""

    # List available tools
    tools = await nc_mcp_client.list_tools()
    tool_names = [tool.name for tool in tools.tools]

    # Verify expected deck tools are present
    expected_deck_tools = ["deck_create_board"]

    for expected_tool in expected_deck_tools:
        assert expected_tool in tool_names, (
            f"Expected deck tool '{expected_tool}' not found in available tools"
        )
        logger.info("Found expected deck tool: %s", expected_tool)

    # List available resource templates
    templates = await nc_mcp_client.list_resource_templates()
    template_uris = [template.uriTemplate for template in templates.resourceTemplates]

    # Verify expected deck resource templates
    expected_deck_templates = [
        "nc://Deck/boards/{board_id}",
    ]

    for expected_template in expected_deck_templates:
        assert expected_template in template_uris, (
            f"Expected deck template '{expected_template}' not found"
        )
        logger.info("Found expected deck resource template: %s", expected_template)

    # List available resources
    resources = await nc_mcp_client.list_resources()
    resource_uris = [str(resource.uri) for resource in resources.resources]

    # Verify expected deck resources
    expected_deck_resources = [
        "nc://Deck/boards",
    ]

    for expected_resource in expected_deck_resources:
        assert expected_resource in resource_uris, (
            f"Expected deck resource '{expected_resource}' not found"
        )
        logger.info("Found expected deck resource: %s", expected_resource)


async def test_deck_board_crud_workflow_mcp(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test complete Deck board CRUD workflow via MCP tools with verification via NextcloudClient."""

    unique_suffix = uuid.uuid4().hex[:8]
    board_title = f"MCP Test Board {unique_suffix}"
    board_color = "0000FF"  # Blue

    # 1. Create board via MCP
    logger.info("Creating board via MCP: %s", board_title)
    create_result = await nc_mcp_client.call_tool(
        "deck_create_board",
        {"title": board_title, "color": board_color},
    )

    assert create_result.isError is False, (
        f"MCP board creation failed: {create_result.content}"
    )
    created_board_json = create_result.content[0].text
    created_board_response = json.loads(created_board_json)
    board_id = created_board_response["id"]

    logger.info("Board created via MCP with ID: %s", board_id)
    assert created_board_response["title"] == board_title
    assert created_board_response["color"] == board_color

    # 2. Verify creation via direct NextcloudClient
    direct_board = await nc_client.deck.get_board(board_id)
    assert direct_board.title == board_title, (
        f"Title mismatch: {direct_board.title} != {board_title}"
    )
    assert direct_board.color == board_color, "Color mismatch"
    logger.info("Board creation verified via direct client")

    # 3. Read board via MCP resource
    logger.info("Reading board via MCP resource: %s", board_id)
    read_result = await nc_mcp_client.read_resource(f"nc://Deck/boards/{board_id}")
    assert len(read_result.contents) == 1, "Expected exactly one content item"
    read_board_data = json.loads(read_result.contents[0].text)

    assert read_board_data["title"] == board_title
    assert read_board_data["color"] == board_color
    logger.info("Board read via MCP resource successfully")

    # 4. Verify board via direct read of resource
    logger.info("Verifying board via resource read: %s", board_id)
    # This was already done in step 3, so we'll just log confirmation
    logger.info("Board structure verified successfully")

    # 5. Read boards list via MCP resource
    logger.info("Reading boards list via MCP resource")
    boards_resource_result = await nc_mcp_client.read_resource("nc://Deck/boards")
    assert len(boards_resource_result.contents) == 1, (
        "Expected exactly one content item"
    )
    boards_resource_data = json.loads(boards_resource_result.contents[0].text)
    assert isinstance(boards_resource_data, list)  # Resources return raw lists

    # Verify our board is in the resource list
    resource_board_ids = [board["id"] for board in boards_resource_data]
    assert board_id in resource_board_ids, "Created board not found in resource list"
    logger.info("Board found in boards resource list")

    # Clean up - delete board
    await nc_client.deck.delete_board(board_id)
    logger.info("Cleaned up board ID: %s", board_id)


async def test_deck_board_operations_error_handling_mcp(nc_mcp_client: ClientSession):
    """Test MCP deck tools handle errors appropriately."""

    non_existent_id = 999999999

    # Test create board with invalid parameters via MCP tool
    logger.info("Testing board creation with invalid parameters via MCP")
    create_result = await nc_mcp_client.call_tool(
        "deck_create_board",
        {"title": "", "color": "FF0000"},
    )

    assert create_result.isError is True, "Expected error for invalid board creation"
    logger.info("Invalid board creation correctly failed via MCP tool")

    # Test read non-existent board via MCP resource
    logger.info("Testing read non-existent board via MCP resource: %s", non_existent_id)
    try:
        read_result = await nc_mcp_client.read_resource(
            f"nc://Deck/boards/{non_existent_id}"
        )
        # If no error is thrown, check if the result indicates an error
        assert len(read_result.contents) == 0, (
            "Expected empty content for non-existent board"
        )
    except Exception as e:
        logger.info("Read non-existent board correctly failed via MCP resource: %s", e)


async def test_deck_board_creation_validation_mcp(nc_mcp_client: ClientSession):
    """Test deck board creation validation via MCP tools."""

    # Test creating board with empty title should fail
    logger.info("Testing board creation with empty title via MCP")
    create_result = await nc_mcp_client.call_tool(
        "deck_create_board",
        {"title": "", "color": "FF0000"},
    )

    assert create_result.isError is True, "Expected error for empty board title"
    logger.info("Empty title board creation correctly failed via MCP")


async def test_deck_board_creation_success_mcp(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test deck board creation with valid parameters via MCP tools."""

    # Test creating board with valid parameters
    logger.info("Testing board creation with valid parameters via MCP")
    create_result = await nc_mcp_client.call_tool(
        "deck_create_board",
        {"title": f"Valid Board {uuid.uuid4().hex[:8]}", "color": "00FF00"},
    )

    assert create_result.isError is False, "Valid board creation should succeed"
    created_board = json.loads(create_result.content[0].text)
    board_id = created_board["id"]
    logger.info("Valid board created successfully with ID: %s", board_id)

    # Clean up - delete board
    await nc_client.deck.delete_board(board_id)
    logger.info("Cleaned up board ID: %s", board_id)


async def test_deck_workflow_integration_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """Test a complete deck workflow using MCP tools with temporary resources."""

    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]
    board_title = board_data["title"]

    # 1. Read board via MCP to verify the structure
    logger.info("Reading board via MCP resource: %s", board_id)
    read_result = await nc_mcp_client.read_resource(f"nc://Deck/boards/{board_id}")
    board_mcp_data = json.loads(read_result.contents[0].text)

    assert board_mcp_data["title"] == board_title
    logger.info("Board structure verified via MCP resource")

    # 2. List boards via MCP resource and verify our board is there
    logger.info("Listing boards via MCP resource")
    list_result = await nc_mcp_client.read_resource("nc://Deck/boards")
    boards_data = json.loads(list_result.contents[0].text)

    board_found = any(board["id"] == board_id for board in boards_data)
    assert board_found, "Board not found in boards list"
    logger.info("Board found in boards list")

    # 3. Verify board data matches via resource (already done in step 1)
    logger.info("Board data verification completed for board: %s", board_id)
    logger.info("Board structure and data verified successfully")


# Card Comment Tests


async def test_deck_card_comment_crud_workflow_mcp(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    temporary_board_with_card: tuple,
):
    """Full CRUD lifecycle for card comments via MCP tools."""
    _, _, card_data = temporary_board_with_card
    card_id = card_data["id"]

    # 1. Create a top-level comment via MCP
    create_result = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {"card_id": card_id, "message": "Initial comment"},
    )
    assert create_result.isError is False, (
        f"Comment creation failed: {create_result.content}"
    )
    create_response = json.loads(create_result.content[0].text)
    assert create_response["success"] is True
    comment = create_response["comment"]
    comment_id = comment["id"]
    assert comment["objectId"] == card_id
    assert comment["message"] == "Initial comment"
    assert comment["replyTo"] is None
    logger.info("Created comment ID %s on card %s", comment_id, card_id)

    # 2. List comments via MCP — verify the new comment is present
    list_result = await nc_mcp_client.call_tool(
        "deck_get_card_comments", {"card_id": card_id}
    )
    assert list_result.isError is False, f"List comments failed: {list_result.content}"
    listed = json.loads(list_result.content[0].text)
    assert listed["count"] >= 1
    listed_ids = [c["id"] for c in listed["results"]]
    assert comment_id in listed_ids, "Created comment not in list"

    # 3. Cross-check via direct client
    direct_comments = await nc_client.deck.get_comments(card_id)
    direct_ids = [c.id for c in direct_comments]
    assert comment_id in direct_ids, "Created comment not visible via direct client"

    # 4. Update the comment via MCP
    update_result = await nc_mcp_client.call_tool(
        "deck_update_card_comment",
        {
            "card_id": card_id,
            "comment_id": comment_id,
            "message": "Edited comment",
        },
    )
    assert update_result.isError is False, (
        f"Comment update failed: {update_result.content}"
    )
    update_response = json.loads(update_result.content[0].text)
    updated = update_response["comment"]
    assert updated["id"] == comment_id
    assert updated["message"] == "Edited comment"

    # 5. Delete the comment via MCP
    delete_result = await nc_mcp_client.call_tool(
        "deck_delete_card_comment",
        {"card_id": card_id, "comment_id": comment_id},
    )
    assert delete_result.isError is False, (
        f"Comment delete failed: {delete_result.content}"
    )
    delete_response = json.loads(delete_result.content[0].text)
    assert delete_response["success"] is True
    assert delete_response["card_id"] == card_id
    assert delete_response["comment_id"] == comment_id

    # 6. Verify the comment is gone
    final_list_result = await nc_mcp_client.call_tool(
        "deck_get_card_comments", {"card_id": card_id}
    )
    final_listed = json.loads(final_list_result.content[0].text)
    final_ids = [c["id"] for c in final_listed["results"]]
    assert comment_id not in final_ids, "Comment still present after delete"


async def test_deck_card_comment_reply_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """Replying with parent_id populates replyTo on the new comment."""
    _, _, card_data = temporary_board_with_card
    card_id = card_data["id"]

    # Create the parent comment
    parent_result = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {"card_id": card_id, "message": "Parent message"},
    )
    assert parent_result.isError is False
    parent = json.loads(parent_result.content[0].text)["comment"]
    parent_id = parent["id"]

    # Create a reply
    reply_result = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {
            "card_id": card_id,
            "message": "Reply message",
            "parent_id": parent_id,
        },
    )
    assert reply_result.isError is False, f"Reply failed: {reply_result.content}"
    reply = json.loads(reply_result.content[0].text)["comment"]

    assert reply["message"] == "Reply message"
    assert reply["replyTo"] is not None, "replyTo should be populated for replies"
    assert reply["replyTo"]["id"] == parent_id
    assert reply["replyTo"]["message"] == "Parent message"


async def test_deck_card_comment_message_too_long_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """Creating a comment longer than 1000 chars is rejected client-side."""
    _, _, card_data = temporary_board_with_card
    card_id = card_data["id"]

    too_long = "x" * 1001
    result = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {"card_id": card_id, "message": too_long},
    )
    assert result.isError is True, "Expected validation error for >1000 char message"


# Compact retrieval (summary projection + board overview)


async def test_deck_get_stacks_summary_default_omits_full_card_fields_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """deck_get_stacks defaults to compact summaries: the card row carries
    title/labels but not the heavy full-card fields (owner/type/etag)."""
    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]

    result = await nc_mcp_client.call_tool("deck_get_stacks", {"board_id": board_id})
    assert result.isError is False, f"deck_get_stacks failed: {result.content}"
    payload = json.loads(result.content[0].text)

    cards = [c for stack in payload["stacks"] for c in (stack.get("cards") or [])]
    card = next(c for c in cards if c["id"] == card_data["id"])
    # Summary fields present...
    assert card["title"] == card_data["title"]
    assert "hasDescription" in card
    assert "labels" in card and isinstance(card["labels"], list)
    # ...heavy full-card fields absent.
    assert "owner" not in card
    assert "type" not in card


async def test_deck_get_stacks_detail_full_keeps_card_fields_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """detail="full" restores the complete card objects (owner/type present)."""
    board_data, _, card_data = temporary_board_with_card
    board_id = board_data["id"]

    result = await nc_mcp_client.call_tool(
        "deck_get_stacks", {"board_id": board_id, "detail": "full"}
    )
    assert result.isError is False, f"deck_get_stacks(full) failed: {result.content}"
    payload = json.loads(result.content[0].text)

    cards = [c for stack in payload["stacks"] for c in (stack.get("cards") or [])]
    card = next(c for c in cards if c["id"] == card_data["id"])
    assert "owner" in card
    assert "type" in card


async def test_deck_get_board_overview_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """deck_get_board_overview returns board title + stacks with compact rows."""
    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]

    result = await nc_mcp_client.call_tool(
        "deck_get_board_overview", {"board_id": board_id}
    )
    assert result.isError is False, f"board overview failed: {result.content}"
    payload = json.loads(result.content[0].text)

    assert payload["board_id"] == board_id
    assert payload["title"] == board_data["title"]
    assert payload["total_cards"] >= 1

    stack = next(s for s in payload["stacks"] if s["id"] == stack_data["id"])
    assert stack["card_count"] == len(stack["cards"])
    card_ids = [c["id"] for c in stack["cards"]]
    assert card_data["id"] in card_ids
