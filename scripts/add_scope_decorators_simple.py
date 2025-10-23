#!/usr/bin/env python3
"""Simpler script to add @require_scopes decorators using regex.

This script uses regex patterns to find @mcp.tool() decorators and adds
the appropriate @require_scopes decorator based on function name patterns.

Usage:
    python scripts/add_scope_decorators_simple.py [--dry-run]
"""

import argparse
import re
from pathlib import Path

# Operation patterns for classification
READ_KEYWORDS = [
    "get",
    "list",
    "search",
    "read",
    "find",
    "fetch",
    "retrieve",
    "upcoming",
]
WRITE_KEYWORDS = [
    "create",
    "update",
    "delete",
    "append",
    "modify",
    "set",
    "add",
    "remove",
    "edit",
    "move",
    "copy",
    "upload",
    "download",
    "share",
    "unshare",
    "bulk",
    "manage",
    "import",
    "reindex",
    "archive",
    "unarchive",
    "reorder",
    "assign",
    "unassign",
    "insert",
    "write",
]


def classify_function(func_name: str) -> str | None:
    """Classify a function name as read or write operation."""
    func_lower = func_name.lower()

    # Check write keywords first (more specific)
    for keyword in WRITE_KEYWORDS:
        if f"_{keyword}_" in func_lower or func_lower.endswith(f"_{keyword}"):
            return "nc:write"

    # Check read keywords
    for keyword in READ_KEYWORDS:
        if f"_{keyword}_" in func_lower or func_lower.endswith(f"_{keyword}"):
            return "nc:read"

    return None


def process_file(file_path: Path, dry_run: bool = False) -> int:
    """Process a single file to add @require_scopes decorators.

    Returns:
        Number of decorators added
    """
    with open(file_path) as f:
        lines = f.readlines()

    # Check if require_scopes is already imported
    has_import = False
    import_line_idx = None

    for i, line in enumerate(lines):
        if "from nextcloud_mcp_server.auth import" in line:
            if "require_scopes" in line:
                has_import = True
            else:
                import_line_idx = i

    modified = False
    decorators_added = 0

    # Find all @mcp.tool() decorators
    i = 0
    while i < len(lines):
        line = lines[i]

        # Look for @mcp.tool() decorator
        if re.match(r"\s*@mcp\.tool\(\)", line):
            # Check if next line already has @require_scopes
            if i + 1 < len(lines) and "@require_scopes" in lines[i + 1]:
                i += 1
                continue

            # Find the function definition (should be on next line or after other decorators)
            func_line_idx = i + 1
            while func_line_idx < len(lines) and not lines[
                func_line_idx
            ].strip().startswith("async def"):
                func_line_idx += 1

            if func_line_idx >= len(lines):
                i += 1
                continue

            # Extract function name
            func_match = re.match(r"\s*async def (\w+)\(", lines[func_line_idx])
            if not func_match:
                i += 1
                continue

            func_name = func_match.group(1)
            scope = classify_function(func_name)

            if scope:
                # Get indentation from @mcp.tool() line
                indent = len(line) - len(line.lstrip())
                decorator_line = " " * indent + f'@require_scopes("{scope}")\n'

                # Insert after @mcp.tool()
                lines.insert(i + 1, decorator_line)
                decorators_added += 1
                modified = True
                print(f'  ✓ {func_name} → @require_scopes("{scope}")')
            else:
                print(f"  ⚠️  Cannot classify: {func_name}")

        i += 1

    # Add import if needed and decorators were added
    if decorators_added > 0 and not has_import:
        if import_line_idx is not None:
            # Add to existing import
            old_line = lines[import_line_idx]
            if old_line.rstrip().endswith(")"):
                lines[import_line_idx] = old_line.rstrip()[:-1] + ", require_scopes)\n"
            else:
                lines[import_line_idx] = old_line.rstrip() + ", require_scopes\n"
            print("  ✓ Added require_scopes to existing import")
            modified = True
        else:
            # No auth import exists, add new import after last 'from nextcloud_mcp_server' import
            last_nc_import_idx = None
            for i, line in enumerate(lines):
                if line.startswith("from nextcloud_mcp_server"):
                    last_nc_import_idx = i

            if last_nc_import_idx is not None:
                lines.insert(
                    last_nc_import_idx + 1,
                    "from nextcloud_mcp_server.auth import require_scopes\n",
                )
                print(
                    "  ✓ Added new import: from nextcloud_mcp_server.auth import require_scopes"
                )
                modified = True
            else:
                print("  ⚠️  Could not find place to add require_scopes import")

    # Write changes
    if modified and not dry_run:
        with open(file_path, "w") as f:
            f.writelines(lines)
        print(f"  💾 Saved changes to {file_path.name}")
    elif dry_run and decorators_added > 0:
        print(f"  🔍 DRY RUN - would add {decorators_added} decorators")

    return decorators_added


def main():
    parser = argparse.ArgumentParser(
        description="Add @require_scopes decorators to MCP tools"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying files",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Process a single file instead of all server modules",
    )
    args = parser.parse_args()

    server_dir = Path(__file__).parent.parent / "nextcloud_mcp_server" / "server"

    if args.file:
        files = [args.file]
    else:
        files = sorted(server_dir.glob("*.py"))
        files = [f for f in files if f.name != "__init__.py"]

    print("🔍 Scanning for tools needing scope decorators...")
    print(
        f"   {'DRY RUN MODE - No changes will be made' if args.dry_run else 'LIVE MODE - Files will be modified'}"
    )

    total_added = 0
    for file_path in files:
        file_path = file_path.resolve()  # Convert to absolute path
        try:
            display_path = file_path.relative_to(Path.cwd())
        except ValueError:
            display_path = file_path.name
        print(f"\n📝 {display_path}")
        added = process_file(file_path, dry_run=args.dry_run)
        total_added += added

    print(f"\n{'📊 Summary (dry run)' if args.dry_run else '✅ Complete'}")
    print(f"   Total decorators added: {total_added}")

    if args.dry_run and total_added > 0:
        print("\n💡 Run without --dry-run to apply changes")


if __name__ == "__main__":
    main()
