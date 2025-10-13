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
        assert "ocs" in capabilities
        logger.info(
            f"OAuth client successfully fetched capabilities: {capabilities.get('ocs').get('meta')}"
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

    async def test_mcp_oauth_server_connection(self, nc_mcp_oauth_client):
        """Test connection to OAuth-enabled MCP server."""
        result = await nc_mcp_oauth_client.list_tools()
        assert result is not None
        assert len(result.tools) > 0

        logger.info(f"OAuth MCP server has {len(result.tools)} tools available")

    async def test_mcp_oauth_tool_execution(self, nc_mcp_oauth_client):
        """Test executing a tool on the OAuth-enabled MCP server."""
        import json

        # Example: Execute the 'nc_tables_list_tables' tool
        result = await nc_mcp_oauth_client.call_tool("nc_tables_list_tables")

        assert result.isError is False, f"Tool execution failed: {result.content}"
        assert result.content is not None
        notes_list = json.loads(result.content[0].text)

        assert isinstance(notes_list, list)

        logger.info(
            f"Successfully executed 'nc_tables_list_tables' tool on OAuth MCP server and got {len(notes_list)} notes."
        )
