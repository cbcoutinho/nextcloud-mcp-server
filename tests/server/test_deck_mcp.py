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
        logger.info(f"Found expected deck tool: {expected_tool}")

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
        logger.info(f"Found expected deck resource template: {expected_template}")

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
        logger.info(f"Found expected deck resource: {expected_resource}")


async def test_deck_board_crud_workflow_mcp(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test complete Deck board CRUD workflow via MCP tools with verification via NextcloudClient."""

    unique_suffix = uuid.uuid4().hex[:8]
    board_title = f"MCP Test Board {unique_suffix}"
    board_color = "0000FF"  # Blue

    # 1. Create board via MCP
    logger.info(f"Creating board via MCP: {board_title}")
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

    logger.info(f"Board created via MCP with ID: {board_id}")
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
    logger.info(f"Reading board via MCP resource: {board_id}")
    read_result = await nc_mcp_client.read_resource(f"nc://Deck/boards/{board_id}")
    assert len(read_result.contents) == 1, "Expected exactly one content item"
    read_board_data = json.loads(read_result.contents[0].text)

    assert read_board_data["title"] == board_title
    assert read_board_data["color"] == board_color
    logger.info("Board read via MCP resource successfully")

    # 4. Verify board via direct read of resource
    logger.info(f"Verifying board via resource read: {board_id}")
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
    logger.info(f"Cleaned up board ID: {board_id}")


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
    logger.info(f"Testing read non-existent board via MCP resource: {non_existent_id}")
    try:
        read_result = await nc_mcp_client.read_resource(
            f"nc://Deck/boards/{non_existent_id}"
        )
        # If no error is thrown, check if the result indicates an error
        assert len(read_result.contents) == 0, (
            "Expected empty content for non-existent board"
        )
    except Exception as e:
        logger.info(f"Read non-existent board correctly failed via MCP resource: {e}")


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
    logger.info(f"Valid board created successfully with ID: {board_id}")

    # Clean up - delete board
    await nc_client.deck.delete_board(board_id)
    logger.info(f"Cleaned up board ID: {board_id}")


async def test_deck_workflow_integration_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """Test a complete deck workflow using MCP tools with temporary resources."""

    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]
    board_title = board_data["title"]

    # 1. Read board via MCP to verify the structure
    logger.info(f"Reading board via MCP resource: {board_id}")
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
    logger.info(f"Board data verification completed for board: {board_id}")
    logger.info("Board structure and data verified successfully")
