#!/bin/bash
# Bump MCP server version
set -e

echo "Bumping MCP server version..."
uv run cz bump --yes

echo "✓ MCP server version bumped"
echo "  - Updated: pyproject.toml, Chart.yaml:appVersion"
echo "  - Tag format: v\${version}"
