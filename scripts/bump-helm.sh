#!/bin/bash
# Bump Helm chart version
set -e

cd charts/nextcloud-mcp-server

echo "Bumping Helm chart version..."
uv run cz --config .cz.toml bump --yes

echo "✓ Helm chart version bumped"
echo "  - Updated: Chart.yaml:version"
echo "  - Tag format: nextcloud-mcp-server-\${version}"
echo "  - Note: appVersion stays at MCP server version"

cd ../..
