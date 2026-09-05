import json
import logging
import re
import uuid

import pytest
from httpx import HTTPStatusError
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
    template_uris = [template.uri_template for template in templates.resource_templates]

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

    assert create_result.is_error is False, (
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

    assert create_result.is_error is True, "Expected error for invalid board creation"
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

    assert create_result.is_error is True, "Expected error for empty board title"
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

    assert create_result.is_error is False, "Valid board creation should succeed"
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
    assert create_result.is_error is False, (
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
    assert list_result.is_error is False, f"List comments failed: {list_result.content}"
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
    assert update_result.is_error is False, (
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
    assert delete_result.is_error is False, (
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
    assert parent_result.is_error is False
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
    assert reply_result.is_error is False, f"Reply failed: {reply_result.content}"
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
    assert result.is_error is True, "Expected validation error for >1000 char message"

    # The error text is the agent-facing contract: it must name the escape
    # hatch, otherwise the caller falls back to guess-and-shrink retries.
    text = result.content[0].text
    assert 'overflow="split"' in text, f"error should name the remedy: {text}"
    assert "1001 characters" in text, f"error should state the exact length: {text}"


def _long_comment_body(repeats: int) -> str:
    """A markdown message that needs several comments to post."""
    block = (
        "## Deploy {n}\n\n"
        "The migration landed and every tenant now resolves its collection "
        "through the registry rather than the hardcoded map. Rollout was "
        "staged over two days and no tenant saw downtime.\n\n"
        "- purged the legacy map\n"
        "- backfilled the audit log\n"
    )
    return "\n".join(block.format(n=i) for i in range(repeats))


async def test_deck_card_comment_split_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """overflow="split" posts an over-length message as one numbered thread."""
    _, _, card_data = temporary_board_with_card
    card_id = card_data["id"]

    message = _long_comment_body(6)
    assert len(message) > 1000, "fixture must actually exceed the limit"

    result = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {"card_id": card_id, "message": message, "overflow": "split"},
    )
    assert result.is_error is False, f"Split comment failed: {result.content}"

    payload = json.loads(result.content[0].text)
    parts = payload["parts"]
    assert payload["part_count"] == len(parts) > 1
    assert parts[0]["id"] == payload["comment"]["id"], "comment must be part 1"

    # Every part is postable on its own terms...
    for index, part in enumerate(parts, 1):
        assert len(part["message"]) <= 1000
        assert part["message"].startswith(f"({index}/{len(parts)}) ")

    # ...and parts 2..N hang off part 1 so the card renders one thread.
    for part in parts[1:]:
        assert part["replyTo"] is not None, "split parts must reply to part 1"
        assert part["replyTo"]["id"] == parts[0]["id"]

    # The thread is really on the card, not just in the response.
    listed = await nc_mcp_client.call_tool(
        "deck_get_card_comments", {"card_id": card_id, "limit": 50}
    )
    listed_ids = {c["id"] for c in json.loads(listed.content[0].text)["results"]}
    assert {p["id"] for p in parts} <= listed_ids

    # Nothing was dropped on the way through.
    rejoined = "".join(re.sub(r"^\(\d+/\d+\) ", "", p["message"]) for p in parts)
    assert re.sub(r"\s+", "", rejoined) == re.sub(r"\s+", "", message)


async def test_deck_card_comment_split_under_parent_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """Part 1 may itself be a reply, making parts 2..N replies to a reply.

    Nextcloud core resolves ``topmostParentId``, so Deck should accept the
    nesting -- but that is worth proving against a real server rather than
    assuming, since the alternative (parts 2..N reusing the caller's parent_id)
    would need a different implementation.
    """
    _, _, card_data = temporary_board_with_card
    card_id = card_data["id"]

    root = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {"card_id": card_id, "message": "Root of the thread"},
    )
    assert root.is_error is False
    root_id = json.loads(root.content[0].text)["comment"]["id"]

    result = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {
            "card_id": card_id,
            "message": _long_comment_body(5),
            "parent_id": root_id,
            "overflow": "split",
        },
    )
    assert result.is_error is False, f"Nested split failed: {result.content}"

    parts = json.loads(result.content[0].text)["parts"]
    assert len(parts) > 1
    assert parts[0]["replyTo"]["id"] == root_id, "part 1 replies to the caller's parent"
    for part in parts[1:]:
        assert part["replyTo"]["id"] == parts[0]["id"]


async def test_deck_card_comment_exactly_1000_chars_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """Core's check is ``> 1000``, so exactly 1000 must be accepted.

    Guards against a future off-by-one turning the boundary into a rejection.
    """
    _, _, card_data = temporary_board_with_card
    card_id = card_data["id"]

    message = "b" * 1000
    result = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {"card_id": card_id, "message": message},
    )
    assert result.is_error is False, f"1000 chars should be legal: {result.content}"

    payload = json.loads(result.content[0].text)
    assert payload["comment"]["message"] == message
    assert payload["part_count"] == 1
    assert payload["parts"] is None


async def test_deck_server_counts_unicode_spaces_php_trim_keeps_mcp(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    temporary_board_with_card: tuple,
):
    """Pin the PHP-vs-Python trim difference against the real server.

    Core trims with PHP ``trim()`` (ASCII whitespace only) before applying
    ``mb_strlen``, whereas Python's ``str.strip()`` also removes Unicode
    Zs spaces such as U+3000. Measuring with the Python default would report
    1000 for the message below, so we would post it and take a 400 back.

    Both halves are asserted here because the claim is about two systems
    agreeing: the server really does reject it, and our client-side guard
    really does catch it first.
    """
    _, _, card_data = temporary_board_with_card
    card_id = card_data["id"]

    message = "b" * 1000 + "　"  # 1001 code points once PHP-trimmed

    # 1. The server rejects it -- proving PHP's trim() keeps the U+3000.
    with pytest.raises(HTTPStatusError) as excinfo:
        await nc_client.deck.create_comment(card_id, message)
    assert excinfo.value.response.status_code == 400

    # 2. And our guard rejects it first, so that 400 never reaches an agent.
    result = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {"card_id": card_id, "message": message},
    )
    assert result.is_error is True
    assert "1001 characters" in result.content[0].text

    # 3. Splitting it works, which it could not if we mismeasured the length.
    split = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {"card_id": card_id, "message": message, "overflow": "split"},
    )
    assert split.is_error is False, (
        f"Split of the padded message failed: {split.content}"
    )
    assert json.loads(split.content[0].text)["part_count"] == 2


async def test_deck_card_comment_update_too_long_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """An over-length update must fail here, never reaching Deck.

    Deck's CommentService::update() lacks create()'s MessageTooLongException
    catch, so the server would answer with a masked 500 whose body says only
    "Internal server error". The client-side guard is what keeps that from
    reaching the agent.
    """
    _, _, card_data = temporary_board_with_card
    card_id = card_data["id"]

    created = await nc_mcp_client.call_tool(
        "deck_create_card_comment",
        {"card_id": card_id, "message": "Short enough to start with"},
    )
    assert created.is_error is False
    comment_id = json.loads(created.content[0].text)["comment"]["id"]

    result = await nc_mcp_client.call_tool(
        "deck_update_card_comment",
        {"card_id": card_id, "comment_id": comment_id, "message": "y" * 1001},
    )
    assert result.is_error is True, "Expected client-side rejection of a long update"

    text = result.content[0].text
    assert "1001 characters" in text
    # Update cannot split, so the error must not send the agent down that path.
    assert "Internal server error" not in text, (
        "the masked 500 must never reach the caller"
    )

    # The original comment is untouched.
    listed = await nc_mcp_client.call_tool(
        "deck_get_card_comments", {"card_id": card_id, "detail": "full"}
    )
    comments = {c["id"]: c for c in json.loads(listed.content[0].text)["results"]}
    assert comments[comment_id]["message"] == "Short enough to start with"


# Compact retrieval (summary projection + board overview)


async def test_deck_get_stacks_summary_default_omits_full_card_fields_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """deck_get_stacks defaults to compact summaries: the card row carries
    title/labels but not the heavy full-card fields (owner/type/etag)."""
    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]

    result = await nc_mcp_client.call_tool("deck_get_stacks", {"board_id": board_id})
    assert result.is_error is False, f"deck_get_stacks failed: {result.content}"
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
    assert result.is_error is False, f"deck_get_stacks(full) failed: {result.content}"
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
    assert result.is_error is False, f"board overview failed: {result.content}"
    payload = json.loads(result.content[0].text)

    assert payload["board_id"] == board_id
    assert payload["title"] == board_data["title"]
    assert payload["total_cards"] >= 1

    stack = next(s for s in payload["stacks"] if s["id"] == stack_data["id"])
    assert stack["card_count"] == len(stack["cards"])
    card_ids = [c["id"] for c in stack["cards"]]
    assert card_data["id"] in card_ids


# Archived-card visibility in the active list tools (issue #842)


async def _create_and_archive_card(
    nc_mcp_client: ClientSession, board_id: int, stack_id: int
) -> int:
    """Create a card in the given stack, archive it, and return its id.

    The card is cleaned up when the temporary stack is deleted by the
    fixture teardown (deleting a stack removes its cards)."""
    create_result = await nc_mcp_client.call_tool(
        "deck_create_card",
        {
            "board_id": board_id,
            "stack_id": stack_id,
            "title": f"Archived Card {uuid.uuid4().hex[:8]}",
        },
    )
    assert create_result.is_error is False, f"create failed: {create_result.content}"
    archived_id = json.loads(create_result.content[0].text)["id"]

    archive_result = await nc_mcp_client.call_tool(
        "deck_archive_card",
        {"board_id": board_id, "stack_id": stack_id, "card_id": archived_id},
    )
    assert archive_result.is_error is False, f"archive failed: {archive_result.content}"
    return archived_id


async def test_deck_get_cards_status_includes_archived_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """deck_get_cards: archived cards appear under status="all"/"archived" and
    are excluded under the default "open" — the regression behind issue #842."""
    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]
    stack_id = stack_data["id"]
    open_id = card_data["id"]
    archived_id = await _create_and_archive_card(nc_mcp_client, board_id, stack_id)

    async def card_ids(status: str) -> list[int]:
        result = await nc_mcp_client.call_tool(
            "deck_get_cards",
            {"board_id": board_id, "stack_id": stack_id, "status": status},
        )
        assert result.is_error is False, f"deck_get_cards({status}) failed"
        return [c["id"] for c in json.loads(result.content[0].text)["cards"]]

    open_ids = await card_ids("open")
    assert open_id in open_ids
    assert archived_id not in open_ids, "archived card must not show under 'open'"

    all_ids = await card_ids("all")
    assert open_id in all_ids and archived_id in all_ids, (
        "status='all' must include both open and archived cards"
    )

    archived_only = await card_ids("archived")
    assert archived_only == [archived_id], (
        f"status='archived' should return only the archived card, got {archived_only}"
    )


async def test_deck_get_stack_status_includes_archived_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """deck_get_stack (single stack) honours archived cards for status
    "all"/"archived" too, mirroring deck_get_cards (issue #842)."""
    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]
    stack_id = stack_data["id"]
    archived_id = await _create_and_archive_card(nc_mcp_client, board_id, stack_id)

    async def stack_card_ids(status: str) -> list[int]:
        result = await nc_mcp_client.call_tool(
            "deck_get_stack",
            {"board_id": board_id, "stack_id": stack_id, "status": status},
        )
        assert result.is_error is False, f"deck_get_stack({status}) failed"
        payload = json.loads(result.content[0].text)
        return [c["id"] for c in (payload.get("cards") or [])]

    open_ids = await stack_card_ids("open")
    assert card_data["id"] in open_ids, "open card must stay visible under 'open'"
    assert archived_id not in open_ids
    assert archived_id in await stack_card_ids("all")
    assert await stack_card_ids("archived") == [archived_id]


async def test_deck_get_stacks_and_overview_include_archived_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_card: tuple
):
    """deck_get_stacks and deck_get_board_overview surface archived cards when
    status="all" (issue #842), keyed onto the correct stack."""
    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]
    stack_id = stack_data["id"]
    archived_id = await _create_and_archive_card(nc_mcp_client, board_id, stack_id)

    async def stacks_card_ids(status: str) -> list[int]:
        result = await nc_mcp_client.call_tool(
            "deck_get_stacks", {"board_id": board_id, "status": status}
        )
        assert result.is_error is False, f"deck_get_stacks({status}) failed"
        payload = json.loads(result.content[0].text)
        stack = next(s for s in payload["stacks"] if s["id"] == stack_id)
        return [c["id"] for c in (stack.get("cards") or [])]

    async def overview_card_ids(status: str) -> list[int]:
        result = await nc_mcp_client.call_tool(
            "deck_get_board_overview", {"board_id": board_id, "status": status}
        )
        assert result.is_error is False, f"board overview({status}) failed"
        payload = json.loads(result.content[0].text)
        stack = next(s for s in payload["stacks"] if s["id"] == stack_id)
        assert stack["card_count"] == len(stack["cards"])
        return [c["id"] for c in stack["cards"]]

    # status="all": both the open and archived card present.
    stacks_all = await stacks_card_ids("all")
    assert card_data["id"] in stacks_all
    assert archived_id in stacks_all, "archived card missing from deck_get_stacks(all)"
    overview_all = await overview_card_ids("all")
    assert archived_id in overview_all, "archived card missing from board overview(all)"

    # status="archived": only the archived card, open card excluded.
    assert await stacks_card_ids("archived") == [archived_id]
    assert await overview_card_ids("archived") == [archived_id]
