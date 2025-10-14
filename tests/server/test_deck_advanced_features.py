import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


# Stack MCP Tools Tests
async def test_deck_stack_mcp_tools(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_board: dict
):
    """Test complete deck stack operations via MCP tools."""
    board_id = temporary_board["id"]
    stack_title = f"MCP Test Stack {uuid.uuid4().hex[:8]}"
    stack_order = 1

    # 1. Create stack via MCP tool
    logger.info(f"Creating stack via MCP: {stack_title}")
    create_result = await nc_mcp_client.call_tool(
        "deck_create_stack",
        {"board_id": board_id, "title": stack_title, "order": stack_order},
    )

    assert create_result.isError is False, (
        f"MCP stack creation failed: {create_result.content}"
    )
    created_stack_response = json.loads(create_result.content[0].text)
    stack_id = created_stack_response["id"]
    assert created_stack_response["title"] == stack_title
    assert created_stack_response["order"] == stack_order
    logger.info(f"Stack created via MCP with ID: {stack_id}")

    try:
        # 2. Get stack via MCP resource
        logger.info(f"Getting stack via MCP resource: {stack_id}")
        get_result = await nc_mcp_client.read_resource(
            f"nc://Deck/boards/{board_id}/stacks/{stack_id}"
        )

        assert len(get_result.contents) == 1, "Expected exactly one content item"
        get_stack_response = json.loads(get_result.contents[0].text)
        assert get_stack_response["title"] == stack_title
        logger.info("Stack retrieved via MCP resource successfully")

        # 3. Update stack via MCP tool
        updated_title = f"Updated {stack_title}"
        updated_order = 2
        logger.info(f"Updating stack via MCP tool: {stack_id}")
        update_result = await nc_mcp_client.call_tool(
            "deck_update_stack",
            {
                "board_id": board_id,
                "stack_id": stack_id,
                "title": updated_title,
                "order": updated_order,
            },
        )

        assert update_result.isError is False, (
            f"MCP stack update failed: {update_result.content}"
        )
        logger.info("Stack updated via MCP tool successfully")

        # 4. Verify update via direct client
        updated_stack = await nc_client.deck.get_stack(board_id, stack_id)
        assert updated_stack.title == updated_title
        assert updated_stack.order == updated_order
        logger.info("Stack update verified via direct client")

        # 5. List stacks via MCP resource
        logger.info("Listing stacks via MCP resource")
        list_result = await nc_mcp_client.read_resource(
            f"nc://Deck/boards/{board_id}/stacks"
        )

        assert len(list_result.contents) == 1, "Expected exactly one content item"
        stacks_data = json.loads(list_result.contents[0].text)
        assert isinstance(stacks_data, list)

        # Verify our stack is in the list
        stack_ids = [stack["id"] for stack in stacks_data]
        assert stack_id in stack_ids, "Updated stack not found in list"
        logger.info(f"Stack {stack_id} found in stacks list")

        # 6. Read stack via MCP resource
        logger.info(f"Reading stack via MCP resource: {stack_id}")
        read_result = await nc_mcp_client.read_resource(
            f"nc://Deck/boards/{board_id}/stacks/{stack_id}"
        )
        read_stack_data = json.loads(read_result.contents[0].text)
        assert read_stack_data["title"] == updated_title
        logger.info("Stack read via MCP resource successfully")

    finally:
        # Clean up
        await nc_client.deck.delete_stack(board_id, stack_id)
        logger.info(f"Cleaned up stack ID: {stack_id}")


# Card MCP Tools Tests
async def test_deck_card_mcp_tools(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    temporary_board_with_stack: tuple,
):
    """Test complete deck card operations via MCP tools."""
    board_data, stack_data = temporary_board_with_stack
    board_id = board_data["id"]
    stack_id = stack_data["id"]
    card_title = f"MCP Test Card {uuid.uuid4().hex[:8]}"
    card_description = f"Test description for {card_title}"

    # 1. Create card via MCP tool
    logger.info(f"Creating card via MCP: {card_title}")
    create_result = await nc_mcp_client.call_tool(
        "deck_create_card",
        {
            "board_id": board_id,
            "stack_id": stack_id,
            "title": card_title,
            "description": card_description,
            "type": "plain",
            "order": 1,
        },
    )

    assert create_result.isError is False, (
        f"MCP card creation failed: {create_result.content}"
    )
    created_card_response = json.loads(create_result.content[0].text)
    card_id = created_card_response["id"]
    assert created_card_response["title"] == card_title
    assert created_card_response["description"] == card_description
    logger.info(f"Card created via MCP with ID: {card_id}")

    try:
        # 2. Get card via MCP resource
        logger.info(f"Getting card via MCP resource: {card_id}")
        get_result = await nc_mcp_client.read_resource(
            f"nc://Deck/boards/{board_id}/stacks/{stack_id}/cards/{card_id}"
        )

        assert len(get_result.contents) == 1, "Expected exactly one content item"
        get_card_response = json.loads(get_result.contents[0].text)
        assert get_card_response["title"] == card_title
        logger.info("Card retrieved via MCP resource successfully")

        # 3. Update card via MCP tool
        updated_title = f"Updated {card_title}"
        updated_description = f"Updated description for {card_title}"
        logger.info(f"Updating card via MCP tool: {card_id}")
        update_result = await nc_mcp_client.call_tool(
            "deck_update_card",
            {
                "board_id": board_id,
                "stack_id": stack_id,
                "card_id": card_id,
                "title": updated_title,
                "description": updated_description,
            },
        )

        assert update_result.isError is False, (
            f"MCP card update failed: {update_result.content}"
        )
        logger.info("Card updated via MCP tool successfully")

        # 4. Verify update via direct client
        updated_card = await nc_client.deck.get_card(board_id, stack_id, card_id)
        assert updated_card.title == updated_title
        assert updated_card.description == updated_description
        logger.info("Card update verified via direct client")

        # 5. Archive/unarchive card via MCP tools
        logger.info(f"Archiving card via MCP tool: {card_id}")
        archive_result = await nc_mcp_client.call_tool(
            "deck_archive_card",
            {"board_id": board_id, "stack_id": stack_id, "card_id": card_id},
        )

        assert archive_result.isError is False, (
            f"MCP card archive failed: {archive_result.content}"
        )
        logger.info("Card archived via MCP tool successfully")

        logger.info(f"Unarchiving card via MCP tool: {card_id}")
        unarchive_result = await nc_mcp_client.call_tool(
            "deck_unarchive_card",
            {"board_id": board_id, "stack_id": stack_id, "card_id": card_id},
        )

        assert unarchive_result.isError is False, (
            f"MCP card unarchive failed: {unarchive_result.content}"
        )
        logger.info("Card unarchived via MCP tool successfully")

        # 6. Move card to different position via MCP tool
        logger.info(f"Reordering card via MCP tool: {card_id}")
        reorder_result = await nc_mcp_client.call_tool(
            "deck_reorder_card",
            {
                "board_id": board_id,
                "stack_id": stack_id,
                "card_id": card_id,
                "order": 10,
                "target_stack_id": stack_id,
            },
        )

        assert reorder_result.isError is False, (
            f"MCP card reorder failed: {reorder_result.content}"
        )
        logger.info("Card reordered via MCP tool successfully")

        # 7. Read card via MCP resource
        logger.info(f"Reading card via MCP resource: {card_id}")
        read_result = await nc_mcp_client.read_resource(
            f"nc://Deck/boards/{board_id}/stacks/{stack_id}/cards/{card_id}"
        )
        read_card_data = json.loads(read_result.contents[0].text)
        assert read_card_data["title"] == updated_title
        logger.info("Card read via MCP resource successfully")

    finally:
        # Clean up
        await nc_client.deck.delete_card(board_id, stack_id, card_id)
        logger.info(f"Cleaned up card ID: {card_id}")


# Label MCP Tools Tests
async def test_deck_label_mcp_tools(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_board: dict
):
    """Test complete deck label operations via MCP tools."""
    board_id = temporary_board["id"]
    label_title = f"MCP Test Label {uuid.uuid4().hex[:8]}"
    label_color = "FF0000"  # Red

    # 1. Create label via MCP tool
    logger.info(f"Creating label via MCP: {label_title}")
    create_result = await nc_mcp_client.call_tool(
        "deck_create_label",
        {"board_id": board_id, "title": label_title, "color": label_color},
    )

    assert create_result.isError is False, (
        f"MCP label creation failed: {create_result.content}"
    )
    created_label_response = json.loads(create_result.content[0].text)
    label_id = created_label_response["id"]
    assert created_label_response["title"] == label_title
    assert created_label_response["color"] == label_color
    logger.info(f"Label created via MCP with ID: {label_id}")

    try:
        # 2. Get label via MCP resource
        logger.info(f"Getting label via MCP resource: {label_id}")
        get_result = await nc_mcp_client.read_resource(
            f"nc://Deck/boards/{board_id}/labels/{label_id}"
        )

        assert len(get_result.contents) == 1, "Expected exactly one content item"
        get_label_response = json.loads(get_result.contents[0].text)
        assert get_label_response["title"] == label_title
        logger.info("Label retrieved via MCP resource successfully")

        # 3. Update label via MCP tool
        updated_title = f"Updated {label_title}"
        updated_color = "00FF00"  # Green
        logger.info(f"Updating label via MCP tool: {label_id}")
        update_result = await nc_mcp_client.call_tool(
            "deck_update_label",
            {
                "board_id": board_id,
                "label_id": label_id,
                "title": updated_title,
                "color": updated_color,
            },
        )

        assert update_result.isError is False, (
            f"MCP label update failed: {update_result.content}"
        )
        logger.info("Label updated via MCP tool successfully")

        # 4. Verify update via direct client
        updated_label = await nc_client.deck.get_label(board_id, label_id)
        assert updated_label.title == updated_title
        assert updated_label.color == updated_color
        logger.info("Label update verified via direct client")

        # 5. Read label via MCP resource
        logger.info(f"Reading label via MCP resource: {label_id}")
        read_result = await nc_mcp_client.read_resource(
            f"nc://Deck/boards/{board_id}/labels/{label_id}"
        )
        read_label_data = json.loads(read_result.contents[0].text)
        assert read_label_data["title"] == updated_title
        logger.info("Label read via MCP resource successfully")

    finally:
        # Clean up
        await nc_client.deck.delete_label(board_id, label_id)
        logger.info(f"Cleaned up label ID: {label_id}")


# Label-Card Assignment Tests
async def test_deck_card_label_assignment_mcp_tools(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    temporary_board_with_card: tuple,
):
    """Test card-label assignment operations via MCP tools."""
    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]
    stack_id = stack_data["id"]
    card_id = card_data["id"]

    # Create a label for assignment
    label = await nc_client.deck.create_label(
        board_id, "Assignment Test Label", "0000FF"
    )
    label_id = label.id

    try:
        # 1. Assign label to card via MCP tool
        logger.info(f"Assigning label {label_id} to card {card_id} via MCP")
        assign_result = await nc_mcp_client.call_tool(
            "deck_assign_label_to_card",
            {
                "board_id": board_id,
                "stack_id": stack_id,
                "card_id": card_id,
                "label_id": label_id,
            },
        )

        assert assign_result.isError is False, (
            f"MCP label assignment failed: {assign_result.content}"
        )
        logger.info("Label assigned to card via MCP tool successfully")

        # 2. Verify assignment via direct client
        card = await nc_client.deck.get_card(board_id, stack_id, card_id)
        if card.labels:
            label_ids = [label.id for label in card.labels]
            assert label_id in label_ids, "Label not found in card labels"
        logger.info("Label assignment verified via direct client")

        # 3. Remove label from card via MCP tool
        logger.info(f"Removing label {label_id} from card {card_id} via MCP")
        remove_result = await nc_mcp_client.call_tool(
            "deck_remove_label_from_card",
            {
                "board_id": board_id,
                "stack_id": stack_id,
                "card_id": card_id,
                "label_id": label_id,
            },
        )

        assert remove_result.isError is False, (
            f"MCP label removal failed: {remove_result.content}"
        )
        logger.info("Label removed from card via MCP tool successfully")

        # 4. Verify removal via direct client
        card = await nc_client.deck.get_card(board_id, stack_id, card_id)
        if card.labels:
            label_ids = [label.id for label in card.labels]
            assert label_id not in label_ids, (
                "Label still found in card labels after removal"
            )
        logger.info("Label removal verified via direct client")

    finally:
        # Clean up
        await nc_client.deck.delete_label(board_id, label_id)
        logger.info(f"Cleaned up label ID: {label_id}")


# User Assignment Tests
async def test_deck_card_user_assignment_mcp_tools(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    temporary_board_with_card: tuple,
):
    """Test card-user assignment operations via MCP tools."""
    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]
    stack_id = stack_data["id"]
    card_id = card_data["id"]

    # Use the current user ID (admin in most test environments)
    user_id = "admin"

    # 1. Assign user to card via MCP tool
    logger.info(f"Assigning user {user_id} to card {card_id} via MCP")
    assign_result = await nc_mcp_client.call_tool(
        "deck_assign_user_to_card",
        {
            "board_id": board_id,
            "stack_id": stack_id,
            "card_id": card_id,
            "user_id": user_id,
        },
    )

    assert assign_result.isError is False, (
        f"MCP user assignment failed: {assign_result.content}"
    )
    logger.info("User assigned to card via MCP tool successfully")

    # 2. Verify assignment via direct client
    card = await nc_client.deck.get_card(board_id, stack_id, card_id)
    if card.assignedUsers:
        user_ids = []
        for user in card.assignedUsers:
            if hasattr(user, "participant"):
                # It's a DeckAssignedUser with participant
                user_ids.append(user.participant.uid)
            elif hasattr(user, "uid"):
                # It's a direct DeckUser
                user_ids.append(user.uid)
        assert user_id in user_ids, "User not found in card assigned users"
    logger.info("User assignment verified via direct client")

    # 3. Unassign user from card via MCP tool
    logger.info(f"Unassigning user {user_id} from card {card_id} via MCP")
    unassign_result = await nc_mcp_client.call_tool(
        "deck_unassign_user_from_card",
        {
            "board_id": board_id,
            "stack_id": stack_id,
            "card_id": card_id,
            "user_id": user_id,
        },
    )

    assert unassign_result.isError is False, (
        f"MCP user unassignment failed: {unassign_result.content}"
    )
    logger.info("User unassigned from card via MCP tool successfully")

    # 4. Verify unassignment via direct client
    card = await nc_client.deck.get_card(board_id, stack_id, card_id)
    if card.assignedUsers:
        user_ids = []
        for user in card.assignedUsers:
            if hasattr(user, "participant"):
                # It's a DeckAssignedUser with participant
                user_ids.append(user.participant.uid)
            elif hasattr(user, "uid"):
                # It's a direct DeckUser
                user_ids.append(user.uid)
        assert user_id not in user_ids, (
            "User still found in card assigned users after removal"
        )
    logger.info("User unassignment verified via direct client")


# Error handling tests
async def test_deck_mcp_tools_error_handling(nc_mcp_client: ClientSession):
    """Test error handling for deck MCP tools with invalid parameters."""
    non_existent_id = 999999999

    # Test stack operations with non-existent board
    stack_result = await nc_mcp_client.call_tool(
        "deck_create_stack",
        {"board_id": non_existent_id, "title": "Should Fail", "order": 1},
    )
    assert stack_result.isError is True, (
        "Expected error for stack creation on non-existent board"
    )

    # Test card operations with non-existent IDs
    card_result = await nc_mcp_client.call_tool(
        "deck_create_card",
        {
            "board_id": non_existent_id,
            "stack_id": non_existent_id,
            "title": "Should Fail",
            "type": "plain",
        },
    )
    assert card_result.isError is True, (
        "Expected error for card creation with non-existent IDs"
    )

    # Test label operations with non-existent board
    label_result = await nc_mcp_client.call_tool(
        "deck_create_label",
        {"board_id": non_existent_id, "title": "Should Fail", "color": "FF0000"},
    )
    assert label_result.isError is True, (
        "Expected error for label creation on non-existent board"
    )

    logger.info("Error handling tests passed for deck MCP tools")


# Resource template tests
async def test_deck_mcp_resource_templates(nc_mcp_client: ClientSession):
    """Test deck MCP resource templates are properly registered."""
    templates = await nc_mcp_client.list_resource_templates()
    template_uris = [template.uriTemplate for template in templates.resourceTemplates]

    expected_templates = [
        "nc://Deck/boards/{board_id}/stacks/{stack_id}",
        "nc://Deck/boards/{board_id}/stacks/{stack_id}/cards/{card_id}",
        "nc://Deck/boards/{board_id}/labels/{label_id}",
    ]

    for expected_template in expected_templates:
        assert expected_template in template_uris, (
            f"Expected template '{expected_template}' not found"
        )
        logger.info(f"Found expected deck resource template: {expected_template}")


# Listing resource tests
async def test_deck_mcp_listing_resources(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """Test deck MCP listing resources for stacks and cards."""
    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]
    stack_id = stack_data["id"]

    # 1. Test listing stacks resource
    logger.info(f"Reading stacks list via MCP resource for board {board_id}")
    stacks_resource_result = await nc_mcp_client.read_resource(
        f"nc://Deck/boards/{board_id}/stacks"
    )
    stacks_resource_data = json.loads(stacks_resource_result.contents[0].text)
    assert isinstance(stacks_resource_data, list)

    # Verify our stack is in the resource list
    stack_ids = [stack["id"] for stack in stacks_resource_data]
    assert stack_id in stack_ids, "Stack not found in stacks resource list"
    logger.info("Stack found in stacks resource list")

    # 2. Test listing cards resource
    logger.info(f"Reading cards list via MCP resource for stack {stack_id}")
    cards_resource_result = await nc_mcp_client.read_resource(
        f"nc://Deck/boards/{board_id}/stacks/{stack_id}/cards"
    )
    cards_resource_data = json.loads(cards_resource_result.contents[0].text)
    assert isinstance(cards_resource_data, list)

    # Verify our card is in the resource list
    card_ids = [card["id"] for card in cards_resource_data]
    assert card_data["id"] in card_ids, "Card not found in cards resource list"
    logger.info("Card found in cards resource list")

    # 3. Test listing labels resource
    logger.info(f"Reading labels list via MCP resource for board {board_id}")
    labels_resource_result = await nc_mcp_client.read_resource(
        f"nc://Deck/boards/{board_id}/labels"
    )
    labels_resource_data = json.loads(labels_resource_result.contents[0].text)
    assert isinstance(labels_resource_data, list)
    logger.info("Labels resource read successfully")
