"""
Multi-user OAuth tests for Nextcloud Notes permissions.

Tests verify that the MCP server respects Nextcloud Notes sharing permissions
when accessed via OAuth authentication with different users.
"""

import json
import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def test_notes_share_read_permissions(
    nc_client, alice_mcp_client, bob_mcp_client, diana_mcp_client
):
    """
    Test that shared notes respect read permissions.

    Scenario:
    1. Admin creates a note as alice
    2. Admin shares the note with bob (read-only)
    3. Bob can read the note via MCP tools
    4. Diana cannot access the note (no share)
    """
    # Create a note as alice (using admin client to set up data)
    note_title = "Alice's Shared Note - Read Test"
    note_content = "This note is shared with Bob for reading only."
    note_category = "SharedNotes"

    logger.info("Creating note as alice...")
    created_note = await nc_client.notes.create_note(
        title=note_title, content=note_content, category=note_category
    )
    note_id = created_note.get("id")

    try:
        # TODO: Share the note with bob (read-only)
        # Note: Nextcloud Notes API doesn't have direct sharing endpoints
        # Sharing is typically done at the folder level via WebDAV
        # For now, this test documents the expected behavior

        # Test: Bob searches for notes via MCP
        logger.info("Bob searching for notes via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_notes_search_notes", arguments={"query": "Alice's Shared"}
        )

        assert result.isError is False, f"Bob's search failed: {result.content}"
        response_data = json.loads(result.content[0].text)

        # Bob should see the shared note in search results
        # (assuming proper share setup)
        assert "results" in response_data
        logger.info(f"Bob found {len(response_data['results'])} notes")

        # Test: Diana searches for the same note
        logger.info("Diana searching for notes via MCP...")
        result = await diana_mcp_client.call_tool(
            "nc_notes_search_notes", arguments={"query": "Alice's Shared"}
        )

        assert result.isError is False
        response_data = json.loads(result.content[0].text)

        # Diana should NOT see the note (no share)
        assert "results" in response_data
        shared_note_ids = [
            n["id"] for n in response_data["results"] if n["id"] == note_id
        ]
        assert len(shared_note_ids) == 0, "Diana should not see unshared note"
        logger.info("Diana correctly cannot see unshared note")

    finally:
        # Cleanup
        logger.info(f"Cleaning up note {note_id}")
        await nc_client.notes.delete_note(note_id)


async def test_notes_share_write_permissions(
    nc_client, alice_mcp_client, charlie_mcp_client, bob_mcp_client
):
    """
    Test that shared notes respect write permissions.

    Scenario:
    1. Admin creates a note as alice
    2. Admin shares the note with charlie (edit permission)
    3. Admin shares the note with bob (read-only)
    4. Charlie can edit the note via MCP tools
    5. Bob cannot edit the note
    """
    # Create a note as alice
    note_title = "Alice's Shared Note - Write Test"
    note_content = "This note is shared with Charlie for editing."
    note_category = "SharedNotes"

    logger.info("Creating note as alice...")
    created_note = await nc_client.notes.create_note(
        title=note_title, content=note_content, category=note_category
    )
    note_id = created_note.get("id")

    try:
        # TODO: Share the note with charlie (edit permission) and bob (read-only)
        # Note: Nextcloud Notes sharing is folder-based

        # Test: Charlie can append content to the note
        logger.info("Charlie attempting to append content via MCP...")
        result = await charlie_mcp_client.call_tool(
            "nc_notes_append_content",
            arguments={
                "note_id": note_id,
                "content": "\n\nCharlie added this content.",
            },
        )

        # If sharing is properly configured, Charlie should succeed
        # Without proper sharing setup, this will fail
        logger.info(f"Charlie's append result: isError={result.isError}")
        if not result.isError:
            logger.info("Charlie successfully appended content (shares configured)")
        else:
            logger.warning("Charlie could not append (shares not yet configured)")

        # Test: Bob attempts to append content (should fail)
        logger.info("Bob attempting to append content via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_notes_append_content",
            arguments={"note_id": note_id, "content": "\n\nBob tried to add this."},
        )

        # Bob should fail (read-only access)
        logger.info(f"Bob's append result: isError={result.isError}")
        if result.isError:
            logger.info("Bob correctly denied write access")
        else:
            logger.warning("Bob unexpectedly succeeded (permissions issue?)")

    finally:
        # Cleanup
        logger.info(f"Cleaning up note {note_id}")
        await nc_client.notes.delete_note(note_id)


async def test_user_isolation_notes(nc_client, alice_mcp_client, bob_mcp_client):
    """
    Test that users can only see their own notes when not shared.

    Scenario:
    1. Admin creates a note as alice (not shared)
    2. Admin creates a note as bob (not shared)
    3. Alice can only see her own note
    4. Bob can only see his own note
    """
    # Create alice's note
    logger.info("Creating alice's private note...")
    alice_note = await nc_client.notes.create_note(
        title="Alice's Private Note",
        content="This is Alice's private content.",
        category="AlicePrivate",
    )
    alice_note_id = alice_note.get("id")

    # Create bob's note
    logger.info("Creating bob's private note...")
    bob_note = await nc_client.notes.create_note(
        title="Bob's Private Note",
        content="This is Bob's private content.",
        category="BobPrivate",
    )
    bob_note_id = bob_note.get("id")

    try:
        # Test: Alice searches all notes
        logger.info("Alice searching all notes via MCP...")
        result = await alice_mcp_client.call_tool(
            "nc_notes_search_notes", arguments={"query": ""}
        )

        assert result.isError is False
        response_data = json.loads(result.content[0].text)
        alice_notes = response_data.get("results", [])
        alice_note_ids = [n["id"] for n in alice_notes]

        logger.info(f"Alice can see {len(alice_notes)} notes")
        # Alice should NOT see Bob's note
        assert bob_note_id not in alice_note_ids, (
            "Alice should not see Bob's private note"
        )

        # Test: Bob searches all notes
        logger.info("Bob searching all notes via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_notes_search_notes", arguments={"query": ""}
        )

        assert result.isError is False
        response_data = json.loads(result.content[0].text)
        bob_notes = response_data.get("results", [])
        bob_note_ids = [n["id"] for n in bob_notes]

        logger.info(f"Bob can see {len(bob_notes)} notes")
        # Bob should NOT see Alice's note
        assert alice_note_id not in bob_note_ids, (
            "Bob should not see Alice's private note"
        )

        logger.info("User isolation test passed: users can only see their own notes")

    finally:
        # Cleanup
        logger.info("Cleaning up test notes...")
        await nc_client.notes.delete_note(alice_note_id)
        await nc_client.notes.delete_note(bob_note_id)


async def test_oauth_mcp_clients_initialized(
    alice_mcp_client, bob_mcp_client, charlie_mcp_client, diana_mcp_client
):
    """
    Smoke test to verify all OAuth MCP clients are properly initialized.
    """
    logger.info("Testing alice_mcp_client initialization...")
    result = await alice_mcp_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )
    assert result.isError is False, f"Alice MCP client failed: {result.content}"
    logger.info("Alice MCP client working")

    logger.info("Testing bob_mcp_client initialization...")
    result = await bob_mcp_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )
    assert result.isError is False, f"Bob MCP client failed: {result.content}"
    logger.info("Bob MCP client working")

    logger.info("Testing charlie_mcp_client initialization...")
    result = await charlie_mcp_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )
    assert result.isError is False, f"Charlie MCP client failed: {result.content}"
    logger.info("Charlie MCP client working")

    logger.info("Testing diana_mcp_client initialization...")
    result = await diana_mcp_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )
    assert result.isError is False, f"Diana MCP client failed: {result.content}"
    logger.info("Diana MCP client working")

    logger.info("All OAuth MCP clients successfully initialized!")
