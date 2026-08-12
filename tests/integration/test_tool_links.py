"""Tool responses carry a usable link back to the content in Nextcloud.

The unit tests pin the URL builders against hand-built models. What they cannot
show is that the real tools reach them with real values — the ids the links are
built from (a note's id, a file's ``file_id``, a card's board) come from live
Nextcloud responses, and the board id in particular is threaded out of either the
enclosing model or the tool's own arguments. So this drives the actual MCP tools
and asserts a link comes back that a browser could open.
"""

import json
import uuid
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.integration


def _payload(result) -> dict:
    """Parsed JSON body of an MCP tool result."""
    assert result.isError is False, f"tool call failed: {result.content}"
    return json.loads(result.content[0].text)


def _assert_openable(url: str | None, expected_path: str) -> None:
    """A link is only useful if a browser can follow it."""
    assert url is not None, "expected a link, got None"
    parsed = urlparse(url)
    assert parsed.scheme in ("http", "https"), f"not browser-openable: {url}"
    assert parsed.netloc, f"no host in: {url}"
    assert parsed.path.endswith(expected_path), (
        f"{url} does not end with {expected_path}"
    )


async def test_note_search_results_link_back_to_each_note(
    nc_mcp_client, temporary_note
):
    """The high-traffic case: every row of a list response is individually linked."""
    note_id = temporary_note["id"]

    result = await nc_mcp_client.call_tool(
        "nc_notes_search_notes", {"query": temporary_note["title"]}
    )
    payload = _payload(result)

    match = next((r for r in payload["results"] if r["id"] == note_id), None)
    assert match is not None, f"note {note_id} not in results"
    _assert_openable(match["url"], f"/apps/notes/note/{note_id}")


async def test_getting_a_single_note_links_to_it(nc_mcp_client, temporary_note):
    note_id = temporary_note["id"]

    payload = _payload(
        await nc_mcp_client.call_tool("nc_notes_get_note", {"note_id": note_id})
    )

    _assert_openable(payload["url"], f"/apps/notes/note/{note_id}")


async def test_creating_a_note_returns_a_link_to_the_new_note(nc_mcp_client):
    """An agent that just created something should be able to point the user at it."""
    unique = uuid.uuid4().hex[:8]
    created = _payload(
        await nc_mcp_client.call_tool(
            "nc_notes_create_note",
            {
                "title": f"Link test {unique}",
                "content": "Created to check the response carries a link.",
                "category": "LinkTest",
            },
        )
    )

    try:
        _assert_openable(created["url"], f"/apps/notes/note/{created['id']}")
    finally:
        await nc_mcp_client.call_tool(
            "nc_notes_delete_note", {"note_id": created["id"]}
        )


async def test_directory_listing_links_files_by_their_file_id(
    nc_mcp_client, temporary_note
):
    """WebDAV links use the /f/{file_id} permalink, so file_id must survive."""
    payload = _payload(
        await nc_mcp_client.call_tool("nc_webdav_list_directory", {"path": ""})
    )

    linkable = [f for f in payload["files"] if f.get("file_id") is not None]
    assert linkable, "no entry carried a file_id, cannot exercise the link"
    for entry in linkable:
        _assert_openable(entry["url"], f"/f/{entry['file_id']}")


async def test_board_overview_links_cards_using_the_boards_id(nc_mcp_client, nc_client):
    """Cards carry no boardId — the link proves the id was threaded from the envelope."""
    boards = await nc_client.deck.get_boards()
    if not boards:
        pytest.skip("No Deck board available")
    board = boards[0]

    payload = _payload(
        await nc_mcp_client.call_tool("deck_get_board_overview", {"board_id": board.id})
    )

    cards = [card for stack in payload["stacks"] for card in stack["cards"]]
    if not cards:
        pytest.skip("Board has no cards to link")

    for card in cards:
        _assert_openable(card["url"], f"/apps/deck/board/{board.id}/card/{card['id']}")


async def test_created_card_is_linked_and_no_longer_echoes_its_description(
    nc_mcp_client, nc_client
):
    """Covers both halves of this change on one call."""
    boards = await nc_client.deck.get_boards()
    if not boards:
        pytest.skip("No Deck board available")
    board = boards[0]
    stacks = await nc_client.deck.get_stacks(board.id)
    if not stacks:
        pytest.skip("Board has no stack to create a card in")
    stack = stacks[0]

    unique = uuid.uuid4().hex[:8]
    created = _payload(
        await nc_mcp_client.call_tool(
            "deck_create_card",
            {
                "board_id": board.id,
                "stack_id": stack.id,
                "title": f"Link test {unique}",
                "description": "A description the caller already knows.",
            },
        )
    )

    try:
        _assert_openable(
            created["url"], f"/apps/deck/board/{board.id}/card/{created['id']}"
        )
        assert "description" not in created, (
            "create responses should not echo the description back"
        )
    finally:
        await nc_mcp_client.call_tool(
            "deck_delete_card",
            {
                "board_id": board.id,
                "stack_id": stack.id,
                "card_id": created["id"],
            },
        )
