#!/bin/bash
# Configure MCP server URL for Astrolabe background sync
# This URL is used by Astrolabe to send app passwords to the MCP server

set -e

# The MCP multi-user BasicAuth service runs on port 8000 inside the container
# From Nextcloud's perspective (inside Docker network), we reach it via service name
MCP_SERVER_URL="${MCP_SERVER_URL:-http://mcp-multi-user-basic:8000}"

echo "Configuring MCP server URL: $MCP_SERVER_URL"

# Set the mcp_server_url in config.php via occ
php occ config:system:set mcp_server_url --value="$MCP_SERVER_URL"

echo "MCP server URL configured successfully"
