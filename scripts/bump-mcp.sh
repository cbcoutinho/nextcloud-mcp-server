#!/bin/bash
# Bump MCP server version
set -euo pipefail

# Parse optional --increment flag
INCREMENT=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --increment)
            INCREMENT="$2"
            shift 2
            ;;
        *)
            echo "❌ Error: Unknown option: $1" >&2
            echo "Usage: $0 [--increment PATCH|MINOR|MAJOR]" >&2
            exit 1
            ;;
    esac
done

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
if [ -n "$INCREMENT" ]; then
    echo "  Forcing $INCREMENT bump"
fi

# Build commitizen command
CZ_CMD="uv run cz bump --yes"
if [ -n "$INCREMENT" ]; then
    CZ_CMD="$CZ_CMD --increment $INCREMENT"
fi

# Run commitizen bump and capture output
if ! output=$($CZ_CMD 2>&1); then
    # Check if this is the expected "no commits to bump" case
    if echo "$output" | grep -q "\[NO_COMMITS_TO_BUMP\]"; then
        echo "ℹ️  No commits eligible for version bump" >&2
        echo "$output" >&2
        exit 0
    fi

    # Otherwise, this is an actual error
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
