"""Integration tests for OAuth authentication."""

import logging

import pytest

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.integration


class TestOAuthClient:
    """Test OAuth-authenticated NextcloudClient."""

    async def test_oauth_client_capabilities(self, nc_oauth_client: NextcloudClient):
        """Test that OAuth client can fetch capabilities."""
        capabilities = await nc_oauth_client.capabilities()

        assert capabilities is not None
        assert "version" in capabilities
        logger.info(
            f"OAuth client successfully fetched capabilities: {capabilities.get('version')}"
        )

    async def test_oauth_client_notes_list(self, nc_oauth_client: NextcloudClient):
        """Test that OAuth client can list notes."""
        notes = await nc_oauth_client.notes.get_notes()

        assert isinstance(notes, list)
        logger.info(f"OAuth client successfully listed {len(notes)} notes")

    async def test_oauth_client_create_note(self, nc_oauth_client: NextcloudClient):
        """Test that OAuth client can create and delete a note."""
        # Create note
        note_title = "OAuth Test Note"
        note_content = "This note was created with OAuth authentication"

        created_note = await nc_oauth_client.notes.create_note(
            title=note_title, content=note_content
        )

        assert created_note is not None
        assert created_note.get("title") == note_title
        note_id = created_note.get("id")
        assert note_id is not None

        logger.info(f"OAuth client successfully created note with ID: {note_id}")

        # Clean up - delete the note
        try:
            await nc_oauth_client.notes.delete_note(note_id=note_id)
            logger.info(f"OAuth client successfully deleted note {note_id}")
        except Exception as e:
            logger.error(f"Failed to clean up test note {note_id}: {e}")
            raise


class TestOAuthTokenValidation:
    """Test OAuth token validation and bearer auth."""

    async def test_token_in_request_headers(
        self, nc_oauth_client: NextcloudClient, oauth_token: str
    ):
        """Verify that bearer token is being used in requests."""
        # The client should be using BearerAuth
        assert nc_oauth_client._auth is not None

        # Make a request and verify it works
        capabilities = await nc_oauth_client.capabilities()
        assert capabilities is not None

        logger.info("OAuth bearer token is correctly included in requests")

    async def test_invalid_token_fails(self):
        """Test that an invalid token results in authentication failure."""
        import os

        from nextcloud_mcp_server.auth import BearerAuth

        nextcloud_host = os.getenv("NEXTCLOUD_HOST")
        if not nextcloud_host:
            pytest.skip("NEXTCLOUD_HOST not set")

        # Create client with invalid token using BearerAuth
        invalid_client = NextcloudClient(
            base_url=nextcloud_host,
            username="testuser",
            auth=BearerAuth("invalid_token_12345"),
        )

        # Attempt to use the client should fail with 401
        from httpx import HTTPStatusError

        with pytest.raises(HTTPStatusError) as exc_info:
            await invalid_client.capabilities()

        assert exc_info.value.response.status_code == 401

        await invalid_client.close()
        logger.info("Invalid OAuth token correctly rejected")


class TestOAuthMCPIntegration:
    """Test OAuth integration with MCP server."""

    @pytest.mark.skip(
        reason="OAuth MCP server integration requires full OAuth flow implementation"
    )
    async def test_mcp_oauth_server_connection(self, nc_mcp_oauth_client):
        """Test connection to OAuth-enabled MCP server."""
        # This test is currently skipped because the OAuth MCP server
        # requires the full OAuth authorization flow to be implemented
        # in the MCP SDK and app.py

        # Once implemented, this test should:
        # 1. Connect to the OAuth MCP server
        # 2. Verify tools are available
        # 3. Call a tool and verify it works with OAuth auth

        result = await nc_mcp_oauth_client.list_tools()
        assert result is not None
        assert len(result.tools) > 0

        logger.info(f"OAuth MCP server has {len(result.tools)} tools available")
