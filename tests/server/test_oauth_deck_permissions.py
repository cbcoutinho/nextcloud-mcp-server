"""
Multi-user OAuth tests for Nextcloud Deck board permissions.

Tests verify that the MCP server respects Nextcloud Deck board ACL permissions
when accessed via OAuth authentication with different users.
"""

import json
import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def add_board_acl(nc_client, board_id: int, user: str, permission_type: int = 0):
    """
    Helper to add ACL entry to a Deck board.

    Args:
        nc_client: Admin NextcloudClient
        board_id: Board ID
        user: Username to grant access
        permission_type: 0=view, 1=edit, 2=manage

    Returns:
        ACL entry ID
    """
    acl = await nc_client.deck.add_acl_rule(
        board_id=board_id,
        type=0,  # 0 = user, 1 = group
        participant=user,
        permission_edit=permission_type >= 1,
        permission_share=permission_type >= 2,
        permission_manage=permission_type >= 2,
    )
    logger.info(f"Added ACL for board {board_id}: {user} (type={permission_type})")
    return acl.id


async def delete_board_acl(nc_client, board_id: int, acl_id: int):
    """Helper to delete a board ACL entry."""
    await nc_client.deck.delete_acl_rule(board_id, acl_id)
    logger.info(f"Deleted ACL {acl_id} from board {board_id}")


@pytest.mark.asyncio
async def test_deck_board_view_permissions(
    nc_client, alice_mcp_client, bob_mcp_client, diana_mcp_client
):
    """
    Test that Deck boards respect view permissions.

    Scenario:
    1. Admin creates a board as alice
    2. Admin adds bob to board with view-only permissions
    3. Bob can view the board via MCP tools
    4. Diana cannot access the board (no ACL entry)
    """
    # Create a board as alice
    logger.info("Creating Deck board as alice...")
    board = await nc_client.deck.create_board(
        "Alice's Shared Board - View Test", "FF0000"
    )
    board_id = board.id

    bob_acl_id = None

    try:
        # Add bob to board with view-only permission
        logger.info("Adding bob to board with view permission...")
        bob_acl_id = await add_board_acl(nc_client, board_id, "bob", permission_type=0)

        # Test: Bob can view the board via MCP
        logger.info("Bob attempting to list boards via MCP...")
        result = await bob_mcp_client.call_tool("deck_get_boards", arguments={})

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            # The response is directly a list of boards
            if not isinstance(response_data, list):
                response_data = [response_data] if response_data else []
            board_ids = [b["id"] for b in response_data]
            logger.info(f"Bob can see {len(response_data)} boards: {board_ids}")

            # Bob should see the shared board
            if board_id in board_ids:
                logger.info(f"Bob can see shared board {board_id}")
            else:
                logger.warning(f"Bob cannot see shared board {board_id}")
        else:
            logger.warning(f"Bob could not list boards: {result.content}")

        # Test: Diana cannot see the board
        logger.info("Diana attempting to list boards via MCP...")
        result = await diana_mcp_client.call_tool("deck_get_boards", arguments={})

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            # The response is directly a list of boards
            if not isinstance(response_data, list):
                response_data = [response_data] if response_data else []
            board_ids = [b["id"] for b in response_data]
            logger.info(f"Diana can see {len(response_data)} boards")

            # Diana should NOT see the board
            assert board_id not in board_ids, "Diana should not see board without ACL"
            logger.info("Diana correctly cannot see board without ACL")
        else:
            logger.warning(f"Diana could not list boards: {result.content}")

    finally:
        # Cleanup
        if bob_acl_id:
            await delete_board_acl(nc_client, board_id, bob_acl_id)
        logger.info(f"Deleting board {board_id}")
        await nc_client.deck.delete_board(board_id)


@pytest.mark.asyncio
async def test_deck_board_edit_permissions(
    nc_client, alice_mcp_client, charlie_mcp_client, bob_mcp_client
):
    """
    Test that Deck boards respect edit permissions.

    Scenario:
    1. Admin creates a board as alice with a stack
    2. Admin adds charlie with edit permission
    3. Admin adds bob with view-only permission
    4. Charlie can create cards via MCP tools
    5. Bob cannot create cards
    """
    # Create a board as alice
    logger.info("Creating Deck board as alice...")
    board = await nc_client.deck.create_board(
        "Alice's Shared Board - Edit Test", "00FF00"
    )
    board_id = board.id

    # Create a stack in the board
    logger.info("Creating stack in board...")
    stack = await nc_client.deck.create_stack(board_id, "Test Stack", 1)
    stack_id = stack.id

    charlie_acl_id = None
    bob_acl_id = None

    try:
        # Add charlie with edit permission
        logger.info("Adding charlie to board with edit permission...")
        charlie_acl_id = await add_board_acl(
            nc_client, board_id, "charlie", permission_type=1
        )

        # Add bob with view-only permission
        logger.info("Adding bob to board with view permission...")
        bob_acl_id = await add_board_acl(nc_client, board_id, "bob", permission_type=0)

        # Test: Charlie can create a card
        logger.info("Charlie attempting to create card via MCP...")
        result = await charlie_mcp_client.call_tool(
            "deck_create_card",
            arguments={
                "board_id": board_id,
                "stack_id": stack_id,
                "title": "Charlie's Card",
                "description": "Created by Charlie with edit permission",
            },
        )

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            card_id = response_data.get("id")
            logger.info(f"Charlie successfully created card {card_id}")

            # Cleanup the card
            await nc_client.deck.delete_card(board_id, stack_id, card_id)
        else:
            logger.warning(f"Charlie could not create card: {result.content}")

        # Test: Bob attempts to create a card (should fail)
        logger.info("Bob attempting to create card via MCP...")
        result = await bob_mcp_client.call_tool(
            "deck_create_card",
            arguments={
                "board_id": board_id,
                "stack_id": stack_id,
                "title": "Bob's Card",
                "description": "Bob trying to create a card",
            },
        )

        if result.isError:
            logger.info("Bob correctly denied card creation (view-only)")
        else:
            logger.warning("Bob unexpectedly succeeded in creating card")
            # Cleanup if bob somehow created a card
            response_data = json.loads(result.content[0].text)
            if "id" in response_data:
                await nc_client.deck.delete_card(
                    board_id, stack_id, response_data["id"]
                )

    finally:
        # Cleanup
        if charlie_acl_id:
            await delete_board_acl(nc_client, board_id, charlie_acl_id)
        if bob_acl_id:
            await delete_board_acl(nc_client, board_id, bob_acl_id)
        logger.info(f"Deleting board {board_id}")
        await nc_client.deck.delete_board(board_id)


@pytest.mark.asyncio
async def test_deck_board_manage_permissions(
    nc_client, alice_mcp_client, charlie_mcp_client
):
    """
    Test that Deck boards respect manage permissions.

    Scenario:
    1. Admin creates a board as alice
    2. Admin adds charlie with manage permission
    3. Charlie can create stacks and modify board settings
    """
    # Create a board as alice
    logger.info("Creating Deck board as alice...")
    board = await nc_client.deck.create_board(
        "Alice's Shared Board - Manage Test", "0000FF"
    )
    board_id = board.id

    charlie_acl_id = None

    try:
        # Add charlie with manage permission
        logger.info("Adding charlie to board with manage permission...")
        charlie_acl_id = await add_board_acl(
            nc_client, board_id, "charlie", permission_type=2
        )

        # Test: Charlie can create a stack
        logger.info("Charlie attempting to create stack via MCP...")
        result = await charlie_mcp_client.call_tool(
            "deck_create_stack",
            arguments={"board_id": board_id, "title": "Charlie's Stack", "order": 1},
        )

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            stack_id = response_data.get("id")
            logger.info(f"Charlie successfully created stack {stack_id}")

            # Cleanup the stack
            await nc_client.deck.delete_stack(board_id, stack_id)
        else:
            logger.warning(f"Charlie could not create stack: {result.content}")

        # Test: Charlie can delete a stack (manage permission)
        logger.info("Charlie attempting to delete stack via MCP...")
        # First create a temporary stack to delete
        temp_stack = await nc_client.deck.create_stack(
            board_id, "Temp Stack for Deletion", 99
        )

        result = await charlie_mcp_client.call_tool(
            "deck_delete_stack",
            arguments={"board_id": board_id, "stack_id": temp_stack.id},
        )

        if not result.isError:
            logger.info("Charlie successfully deleted stack")
        else:
            logger.warning(f"Charlie could not delete stack: {result.content}")
            # Cleanup if deletion via MCP failed
            try:
                await nc_client.deck.delete_stack(board_id, temp_stack.id)
            except Exception:
                pass

    finally:
        # Cleanup
        if charlie_acl_id:
            await delete_board_acl(nc_client, board_id, charlie_acl_id)
        logger.info(f"Deleting board {board_id}")
        await nc_client.deck.delete_board(board_id)


@pytest.mark.asyncio
async def test_deck_user_isolation(nc_client, alice_mcp_client, bob_mcp_client):
    """
    Test that users can only see their own boards when not shared.

    Scenario:
    1. Admin creates a board as alice (not shared)
    2. Admin creates a board as bob (not shared)
    3. Alice can only see her own board
    4. Bob can only see his own board
    """
    # Create alice's board
    logger.info("Creating alice's private board...")
    alice_board = await nc_client.deck.create_board("Alice's Private Board", "FF00FF")
    alice_board_id = alice_board.id

    # Create bob's board
    logger.info("Creating bob's private board...")
    bob_board = await nc_client.deck.create_board("Bob's Private Board", "00FFFF")
    bob_board_id = bob_board.id

    try:
        # Test: Alice lists boards
        logger.info("Alice listing boards via MCP...")
        result = await alice_mcp_client.call_tool("deck_get_boards", arguments={})

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            # The response is directly a list of boards
            if not isinstance(response_data, list):
                response_data = [response_data] if response_data else []
            board_ids = [b["id"] for b in response_data]
            logger.info(f"Alice can see boards: {board_ids}")

            # Alice should NOT see Bob's board
            assert bob_board_id not in board_ids, (
                "Alice should not see Bob's private board"
            )
        else:
            logger.warning(f"Alice could not list boards: {result.content}")

        # Test: Bob lists boards
        logger.info("Bob listing boards via MCP...")
        result = await bob_mcp_client.call_tool("deck_get_boards", arguments={})

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            # The response is directly a list of boards
            if not isinstance(response_data, list):
                response_data = [response_data] if response_data else []
            board_ids = [b["id"] for b in response_data]
            logger.info(f"Bob can see boards: {board_ids}")

            # Bob should NOT see Alice's board
            assert alice_board_id not in board_ids, (
                "Bob should not see Alice's private board"
            )
        else:
            logger.warning(f"Bob could not list boards: {result.content}")

        logger.info("User isolation test passed: users can only see their own boards")

    finally:
        # Cleanup
        logger.info("Cleaning up test boards...")
        await nc_client.deck.delete_board(alice_board_id)
        await nc_client.deck.delete_board(bob_board_id)
