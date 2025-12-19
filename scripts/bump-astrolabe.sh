#!/bin/bash
# Bump Astrolabe app version
set -e

cd third_party/astrolabe

echo "Bumping Astrolabe version..."
uv run cz --config .cz.toml bump --yes

echo "✓ Astrolabe version bumped"
echo "  - Updated: appinfo/info.xml, package.json"
echo "  - Tag format: astrolabe-v\${version}"

cd ../..
