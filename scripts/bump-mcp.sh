#!/bin/bash
# Bump MCP server version
set -euo pipefail

# Validate dependencies
command -v uv >/dev/null 2>&1 || {
    echo "❌ Error: uv not found" >&2
    echo "   Install from https://docs.astral.sh/uv/" >&2
    exit 1
}

# Validate we're in the repository root
if [ ! -f "pyproject.toml" ]; then
    echo "❌ Error: Must run from repository root (pyproject.toml not found)" >&2
    exit 1
fi

echo "Bumping MCP server version..."

# Run commitizen bump and capture output
if ! output=$(uv run cz bump --yes 2>&1); then
    echo "❌ Error: Version bump failed" >&2
    echo "$output" >&2
    echo "" >&2
    echo "Common causes:" >&2
    echo "  - No commits since last version" >&2
    echo "  - No conventional commits found (use feat:, fix:, etc.)" >&2
    echo "  - Git working directory not clean" >&2
    exit 1
fi

echo "$output"
echo ""
echo "✓ MCP server version bumped successfully"
echo "  Updated: pyproject.toml, Chart.yaml:appVersion"
echo "  Tag format: v\${version}"
echo ""
echo "Next steps:"
echo "  git push --follow-tags"
