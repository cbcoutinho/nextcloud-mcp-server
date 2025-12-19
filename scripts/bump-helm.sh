#!/bin/bash
# Bump Helm chart version
set -e

# Validate dependencies
command -v uv >/dev/null 2>&1 || { echo "Error: uv not found. Install from https://docs.astral.sh/uv/"; exit 1; }

cd charts/nextcloud-mcp-server

echo "Bumping Helm chart version..."
uv run cz --config .cz.toml bump --yes

echo "✓ Helm chart version bumped"
echo "  - Updated: Chart.yaml:version"
echo "  - Tag format: nextcloud-mcp-server-\${version}"
echo "  - Note: appVersion stays at MCP server version"

cd ../..
