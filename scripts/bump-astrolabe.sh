#!/bin/bash
# Bump Astrolabe app version
set -e

# Validate dependencies
command -v uv >/dev/null 2>&1 || { echo "Error: uv not found. Install from https://docs.astral.sh/uv/"; exit 1; }

cd third_party/astrolabe

echo "Bumping Astrolabe version..."
uv run cz --config .cz.toml bump --yes

echo "✓ Astrolabe version bumped"
echo "  - Updated: appinfo/info.xml, package.json"
echo "  - Tag format: astrolabe-v\${version}"

cd ../..
