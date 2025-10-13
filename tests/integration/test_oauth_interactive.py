"""Interactive integration tests for OAuth authentication."""

import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.interactive]


class TestOAuthInteractive:
    """Test interactive OAuth authentication."""

    async def test_oauth_client_with_interactive_flow(self, nc_oauth_client):
        """Test that OAuth client created via interactive flow can access Nextcloud APIs."""
        # Test 1: Check capabilities
        capabilities = await nc_oauth_client.capabilities()
        assert capabilities is not None
        logger.info("OAuth client (interactive) successfully fetched capabilities")

        # Test 2: List notes
        notes = await nc_oauth_client.notes.get_all_notes()
        assert isinstance(notes, list)
        logger.info(
            f"OAuth client (interactive) successfully listed {len(notes)} notes"
        )

        # Test 3: Create and delete a note
        test_note = await nc_oauth_client.notes.create_note(
            title="OAuth Interactive Test Note",
            content="This note was created during OAuth interactive testing",
        )
        assert test_note is not None
        assert test_note.get("id") is not None
        note_id = test_note["id"]
        logger.info(f"OAuth client (interactive) successfully created note {note_id}")

        # Clean up
        await nc_oauth_client.notes.delete_note(note_id=note_id)
        logger.info(f"OAuth client (interactive) successfully deleted note {note_id}")
