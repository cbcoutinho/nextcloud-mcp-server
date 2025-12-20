#!/bin/bash
# Bump Astrolabe app version
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

# Validate Astrolabe directory exists
if [ ! -d "third_party/astrolabe" ]; then
    echo "❌ Error: Must run from repository root (third_party/astrolabe not found)" >&2
    exit 1
fi

cd third_party/astrolabe

# Validate required files exist
if [ ! -f "appinfo/info.xml" ]; then
    echo "❌ Error: appinfo/info.xml not found" >&2
    exit 1
fi

if [ ! -f "package.json" ]; then
    echo "❌ Error: package.json not found" >&2
    exit 1
fi

echo "Bumping Astrolabe version..."
if [ -n "$INCREMENT" ]; then
    echo "  Forcing $INCREMENT bump"
fi

# Build commitizen command
CZ_CMD="uv run cz --config .cz.toml bump --yes"
if [ -n "$INCREMENT" ]; then
    CZ_CMD="$CZ_CMD --increment $INCREMENT"
fi

# Run commitizen bump and capture output
if ! output=$($CZ_CMD 2>&1); then
    cd ../..

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
    echo "  - No commits with scope 'astrolabe' since last version" >&2
    echo "  - No conventional commits found (use feat(astrolabe):, fix(astrolabe):, etc.)" >&2
    echo "  - Git working directory not clean" >&2
    exit 1
fi

echo "$output"
echo ""
echo "✓ Astrolabe version bumped successfully"
echo "  Updated: appinfo/info.xml, package.json"
echo "  Tag format: astrolabe-v\${version}"
echo ""
echo "Next steps:"
echo "  cd ../.."
echo "  git push --follow-tags"

cd ../..
