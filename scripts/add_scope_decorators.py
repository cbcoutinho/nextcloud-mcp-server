#!/usr/bin/env python3
"""Script to automatically add @require_scopes decorators to MCP tools.

This script parses server module files and adds appropriate scope decorators
based on the operation type (read vs write).

Usage:
    python scripts/add_scope_decorators.py [--dry-run] [--file FILE]
"""

import argparse
import ast
import re
from pathlib import Path
from typing import List, Tuple

# Operation patterns for classification
READ_PATTERNS = [
    r".*_get_.*",
    r".*_get$",
    r".*_list_.*",
    r".*_list$",
    r".*_search_.*",
    r".*_search$",
    r".*_read_.*",
    r".*_read$",
    r".*_find_.*",
    r".*_find$",
    r".*_fetch_.*",
    r".*_fetch$",
    r".*_retrieve_.*",
    r".*_retrieve$",
]

WRITE_PATTERNS = [
    r".*_create_.*",
    r".*_create$",
    r".*_update_.*",
    r".*_update$",
    r".*_delete_.*",
    r".*_delete$",
    r".*_append_.*",
    r".*_append$",
    r".*_modify_.*",
    r".*_modify$",
    r".*_set_.*",
    r".*_set$",
    r".*_add_.*",
    r".*_add$",
    r".*_remove_.*",
    r".*_remove$",
    r".*_edit_.*",
    r".*_edit$",
    r".*_move_.*",
    r".*_move$",
    r".*_copy_.*",
    r".*_copy$",
    r".*_upload_.*",
    r".*_upload$",
    r".*_download_.*",
    r".*_download$",
    r".*_share_.*",
    r".*_share$",
    r".*_unshare_.*",
    r".*_unshare$",
    r".*_bulk_.*",  # Bulk operations are typically writes
]


def classify_operation(func_name: str) -> str | None:
    """Classify a function as read or write operation.

    Args:
        func_name: Function name to classify

    Returns:
        "nc:read", "nc:write", or None if cannot classify
    """
    # Check write patterns first (more specific)
    for pattern in WRITE_PATTERNS:
        if re.match(pattern, func_name):
            return "nc:write"

    # Check read patterns
    for pattern in READ_PATTERNS:
        if re.match(pattern, func_name):
            return "nc:read"

    return None


def has_scope_decorator(decorators: List[ast.expr]) -> bool:
    """Check if function already has @require_scopes decorator."""
    for decorator in decorators:
        if isinstance(decorator, ast.Call):
            if (
                isinstance(decorator.func, ast.Name)
                and decorator.func.id == "require_scopes"
            ):
                return True
        elif isinstance(decorator, ast.Name) and decorator.name == "require_scopes":
            return True
    return False


def has_mcp_tool_decorator(decorators: List[ast.expr]) -> bool:
    """Check if function has @mcp.tool() decorator."""
    for decorator in decorators:
        if isinstance(decorator, ast.Call):
            if isinstance(decorator.func, ast.Attribute):
                if decorator.func.attr == "tool":
                    return True
    return False


def find_tools_needing_decorators(
    file_path: Path, verbose: bool = False
) -> List[Tuple[str, int, str]]:
    """Find all tools that need scope decorators.

    Returns:
        List of (function_name, line_number, required_scope)
    """
    with open(file_path) as f:
        content = f.read()

    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        print(f"  ⚠️  Syntax error in {file_path}: {e}")
        return []

    tools_to_update = []
    total_functions = 0
    mcp_tools = 0
    already_has_scope = 0
    cannot_classify = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            total_functions += 1

            if verbose and node.decorator_list:
                decorators_str = [
                    ast.unparse(d) if hasattr(ast, "unparse") else str(d)
                    for d in node.decorator_list
                ]
                print(f"  Function {node.name} has decorators: {decorators_str}")

            # Check if it's an MCP tool
            if not has_mcp_tool_decorator(node.decorator_list):
                continue

            mcp_tools += 1

            # Check if it already has scope decorator
            if has_scope_decorator(node.decorator_list):
                already_has_scope += 1
                continue

            # Classify operation
            scope = classify_operation(node.name)
            if scope:
                tools_to_update.append((node.name, node.lineno, scope))
            else:
                cannot_classify += 1
                if verbose:
                    print(f"  ⚠️  Cannot classify: {node.name}")

    if verbose:
        print(
            f"  Debug: total_functions={total_functions}, mcp_tools={mcp_tools}, already_has_scope={already_has_scope}, cannot_classify={cannot_classify}"
        )

    return tools_to_update


def add_decorator_to_file(
    file_path: Path, dry_run: bool = False, verbose: bool = False
) -> int:
    """Add @require_scopes decorators to tools in a file.

    Returns:
        Number of decorators added
    """
    tools = find_tools_needing_decorators(file_path, verbose=verbose)

    if not tools:
        return 0

    print(f"\n📝 {file_path.relative_to(Path.cwd())}")

    with open(file_path) as f:
        lines = f.readlines()

    # Check if require_scopes is already imported
    has_import = False
    import_line_idx = None
    for i, line in enumerate(lines):
        if "from nextcloud_mcp_server.auth import" in line and "require_scopes" in line:
            has_import = True
            break
        elif "from nextcloud_mcp_server.auth import" in line:
            import_line_idx = i

    # Add import if needed
    if not has_import:
        if import_line_idx is not None:
            # Add require_scopes to existing import
            old_line = lines[import_line_idx]
            if "(" in old_line:
                # Multi-line import
                print(
                    "  ⚠️  Multi-line import detected, please add manually: from nextcloud_mcp_server.auth import require_scopes"
                )
            else:
                # Single line import - add require_scopes
                lines[import_line_idx] = (
                    old_line.rstrip().rstrip(")").rstrip() + ", require_scopes)\n"
                )
                print("  ✓ Added require_scopes to import")
        else:
            # No auth import exists, add new import
            # Find first import line
            for i, line in enumerate(lines):
                if line.startswith("from nextcloud_mcp_server"):
                    lines.insert(
                        i, "from nextcloud_mcp_server.auth import require_scopes\n"
                    )
                    print(
                        "  ✓ Added import: from nextcloud_mcp_server.auth import require_scopes"
                    )
                    break

    # Add decorators to tools (in reverse order to preserve line numbers)
    for func_name, line_num, scope in reversed(tools):
        # Find the @mcp.tool() decorator line
        for i in range(line_num - 1, max(0, line_num - 10), -1):
            if "@mcp.tool()" in lines[i]:
                # Get indentation from @mcp.tool() line
                indent = len(lines[i]) - len(lines[i].lstrip())
                decorator_line = " " * indent + f'@require_scopes("{scope}")\n'
                lines.insert(i + 1, decorator_line)
                print(f'  ✓ {func_name}:{line_num} → @require_scopes("{scope}")')
                break

    if not dry_run:
        with open(file_path, "w") as f:
            f.writelines(lines)
        print("  💾 Saved changes")
    else:
        print("  🔍 DRY RUN - no changes written")

    return len(tools)


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
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show debug information",
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
        added = add_decorator_to_file(
            file_path, dry_run=args.dry_run, verbose=args.verbose
        )
        total_added += added

    print(f"\n{'📊 Summary (dry run)' if args.dry_run else '✅ Complete'}")
    print(f"   Total decorators added: {total_added}")

    if args.dry_run:
        print("\n💡 Run without --dry-run to apply changes")


if __name__ == "__main__":
    main()
