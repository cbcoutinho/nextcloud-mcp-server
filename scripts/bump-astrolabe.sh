#!/bin/bash
# Bump Astrolabe app version
set -euo pipefail

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

# Run commitizen bump and capture output
if ! output=$(uv run cz --config .cz.toml bump --yes 2>&1); then
    cd ../..
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
