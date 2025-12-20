"""Test Astrolabe integration with multiple MCP server deployments.

This test suite verifies that the Astrolabe app can be dynamically configured
to connect to different MCP server deployments (mcp-oauth, mcp-keycloak, etc.).

The configuration is managed dynamically during tests using the
configure_astrolabe_for_mcp_server fixture, which allows testing multiple
deployment scenarios without requiring static post-installation configuration.
"""

import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


class TestAstrolabeMultiServerIntegration:
    """Test suite for Astrolabe integration with multiple MCP servers."""

    @pytest.mark.parametrize(
        "mcp_server_config",
        [
            {
                "name": "mcp-oauth",
                "internal_url": "http://mcp-oauth:8001",
                "public_url": "http://localhost:8001",
            },
            {
                "name": "mcp-keycloak",
                "internal_url": "http://mcp-keycloak:8002",
                "public_url": "http://localhost:8002",
            },
            # Add more MCP server configurations as needed:
            # {
            #     "name": "mcp-multi-user-basic",
            #     "internal_url": "http://mcp-multi-user-basic:8000",
            #     "public_url": "http://localhost:8003",
            # },
        ],
    )
    async def test_astrolabe_configuration_for_different_servers(
        self, configure_astrolabe_for_mcp_server, mcp_server_config
    ):
        """Test that Astrolabe can be configured for different MCP servers.

        This test verifies that:
        1. The configure_astrolabe_for_mcp_server fixture successfully configures
           the Astrolabe app for different MCP server endpoints
        2. OAuth client credentials are properly generated and stored
        3. The configuration can be dynamically changed between tests
        """
        logger.info(f"Configuring Astrolabe for {mcp_server_config['name']}...")

        # Configure Astrolabe for the specific MCP server
        credentials = await configure_astrolabe_for_mcp_server(
            mcp_server_internal_url=mcp_server_config["internal_url"],
            mcp_server_public_url=mcp_server_config["public_url"],
        )

        # Verify credentials were returned
        assert "client_id" in credentials
        assert "client_secret" in credentials
        assert credentials["client_id"] == "nextcloudMcpServerUIPublicClient"
        assert len(credentials["client_secret"]) > 0

        logger.info(
            f"✓ Astrolabe successfully configured for {mcp_server_config['name']}"
        )
        logger.info(f"  Internal URL: {mcp_server_config['internal_url']}")
        logger.info(f"  Public URL: {mcp_server_config['public_url']}")
        logger.info(f"  Client ID: {credentials['client_id']}")
        logger.info(f"  Client Secret: {credentials['client_secret'][:8]}...")

    async def test_astrolabe_reconfiguration(self, configure_astrolabe_for_mcp_server):
        """Test that Astrolabe can be reconfigured multiple times in the same session.

        This verifies that the OAuth client can be recreated with different
        settings without conflicts.
        """
        # First configuration: mcp-oauth
        logger.info("First configuration: mcp-oauth")
        credentials1 = await configure_astrolabe_for_mcp_server(
            mcp_server_internal_url="http://mcp-oauth:8001",
            mcp_server_public_url="http://localhost:8001",
        )

        assert credentials1["client_id"] == "nextcloudMcpServerUIPublicClient"

        # Second configuration: mcp-keycloak (reconfiguration)
        logger.info("Second configuration: mcp-keycloak (reconfiguration)")
        credentials2 = await configure_astrolabe_for_mcp_server(
            mcp_server_internal_url="http://mcp-keycloak:8002",
            mcp_server_public_url="http://localhost:8002",
        )

        assert credentials2["client_id"] == "nextcloudMcpServerUIPublicClient"

        # Client secrets should be different (new client created)
        assert credentials1["client_secret"] != credentials2["client_secret"]

        logger.info("✓ Astrolabe successfully reconfigured without conflicts")
