#!/bin/bash
# Bump Helm chart version
set -euo pipefail

# Validate dependencies
command -v uv >/dev/null 2>&1 || {
    echo "❌ Error: uv not found" >&2
    echo "   Install from https://docs.astral.sh/uv/" >&2
    exit 1
}

# Validate Helm chart directory exists
if [ ! -d "charts/nextcloud-mcp-server" ]; then
    echo "❌ Error: Must run from repository root (charts/ not found)" >&2
    exit 1
fi

cd charts/nextcloud-mcp-server

# Validate Chart.yaml exists
if [ ! -f "Chart.yaml" ]; then
    echo "❌ Error: Chart.yaml not found" >&2
    exit 1
fi

echo "Bumping Helm chart version..."

# Run commitizen bump and capture output
if ! output=$(uv run cz --config .cz.toml bump --yes 2>&1); then
    cd ../..
    echo "❌ Error: Version bump failed" >&2
    echo "$output" >&2
    echo "" >&2
    echo "Common causes:" >&2
    echo "  - No commits with scope 'helm' since last version" >&2
    echo "  - No conventional commits found (use feat(helm):, fix(helm):, etc.)" >&2
    echo "  - Git working directory not clean" >&2
    exit 1
fi

echo "$output"
echo ""
echo "✓ Helm chart version bumped successfully"
echo "  Updated: Chart.yaml:version"
echo "  Tag format: nextcloud-mcp-server-\${version}"
echo "  Note: appVersion stays at MCP server version"
echo ""
echo "Next steps:"
echo "  cd ../.."
echo "  git push --follow-tags"

cd ../..
