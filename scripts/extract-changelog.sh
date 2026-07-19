#!/bin/bash
# Extract the CHANGELOG.md section for a single version, for use as GitHub
# Release notes.
#
# commitizen writes headings as "## v0.142.0 (2026-07-19)" (see
# [tool.commitizen] update_changelog_on_bump in pyproject.toml), so a section
# runs from its own heading up to the next "## " heading.
set -euo pipefail

TAG="${1:?Usage: $0 <tag>   e.g. $0 v0.142.0}"
CHANGELOG="${2:-CHANGELOG.md}"

if [ ! -f "$CHANGELOG" ]; then
    echo "❌ Error: $CHANGELOG not found (run from repository root)" >&2
    exit 1
fi

# Match the heading by exact string comparison rather than a regex: a version
# like v0.142.0 is full of dots, which awk would treat as "any character" in a
# dynamic regex and could match a neighbouring version.
section=$(awk -v tag="$TAG" '
    BEGIN { heading = "## " tag; dated = heading " (" }
    !found && ($0 == heading || substr($0, 1, length(dated)) == dated) { found = 1; next }
    found && substr($0, 1, 3) == "## " { exit }
    found { print }
' "$CHANGELOG")

# Strip leading and trailing blank lines so the release body starts on content.
section=$(printf '%s\n' "$section" | sed -e '/./,$!d' | tac | sed -e '/./,$!d' | tac)

if [ -z "$section" ]; then
    echo "❌ Error: no CHANGELOG section found for $TAG in $CHANGELOG" >&2
    exit 1
fi

printf '%s\n' "$section"
