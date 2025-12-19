#!/bin/bash
# Bump MCP server version
set -e

# Validate dependencies
command -v uv >/dev/null 2>&1 || { echo "Error: uv not found. Install from https://docs.astral.sh/uv/"; exit 1; }

echo "Bumping MCP server version..."
uv run cz bump --yes

echo "✓ MCP server version bumped"
echo "  - Updated: pyproject.toml, Chart.yaml:appVersion"
echo "  - Tag format: v\${version}"
