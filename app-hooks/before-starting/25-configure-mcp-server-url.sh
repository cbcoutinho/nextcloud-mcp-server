#!/bin/bash
# Configure MCP server URL for Astrolabe background sync
# This URL is used by Astrolabe to send app passwords to the MCP server

set -e

if [ -z "${MCP_SERVER_URL:-}" ]; then
  echo "MCP_SERVER_URL not set, skipping Astrolabe MCP server URL configuration"
  exit 0
fi

echo "Configuring MCP server URL: $MCP_SERVER_URL"

# Set the mcp_server_url in config.php via occ
php occ config:system:set mcp_server_url --value="$MCP_SERVER_URL"

echo "MCP server URL configured successfully"
