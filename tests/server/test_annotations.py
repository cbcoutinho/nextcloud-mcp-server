"""Tests for MCP tool annotations (ADR-017)."""

import pytest
from mcp import ClientSession

pytestmark = pytest.mark.integration


async def test_all_tools_have_titles(nc_mcp_client: ClientSession):
    """Verify all tools have human-readable titles (Phase 1 of ADR-017)."""
    tools = await nc_mcp_client.list_tools()

    # Every tool should have a title (not None)
    for tool in tools.tools:
        assert tool.title is not None, f"Tool {tool.name} is missing a title"
        # Title should not be empty
        assert tool.title.strip() != "", f"Tool {tool.name} has an empty title"
        # Title should be human-readable (not snake_case function name)
        assert tool.title != tool.name, (
            f"Tool {tool.name} title is same as function name"
        )


async def test_all_tools_have_annotations(nc_mcp_client: ClientSession):
    """Verify all tools have ToolAnnotations (Phase 2 of ADR-017)."""
    tools = await nc_mcp_client.list_tools()

    for tool in tools.tools:
        # Every tool should have annotations
        assert tool.annotations is not None, f"Tool {tool.name} is missing annotations"


async def test_read_only_tools_have_correct_annotations(nc_mcp_client: ClientSession):
    """Verify read-only tools are marked correctly."""
    tools = await nc_mcp_client.list_tools()

    # Known read-only tools (list, search, get operations)
    read_only_prefixes = ["list", "search", "get"]
    read_only_patterns = ["_get_", "_list_", "_search_"]

    for tool in tools.tools:
        # Check if tool name suggests it's read-only
        is_likely_readonly = tool.name.startswith(tuple(read_only_prefixes)) or any(
            pattern in tool.name for pattern in read_only_patterns
        )

        if is_likely_readonly:
            assert tool.annotations is not None, f"Tool {tool.name} missing annotations"
            assert tool.annotations.readOnlyHint is True, (
                f"Read-only tool {tool.name} should have readOnlyHint=True"
            )
            assert tool.annotations.destructiveHint is not True, (
                f"Read-only tool {tool.name} should not have destructiveHint=True"
            )


async def test_destructive_tools_have_correct_annotations(nc_mcp_client: ClientSession):
    """Verify destructive operations are marked correctly."""
    tools = await nc_mcp_client.list_tools()

    # Known destructive operations
    destructive_keywords = ["delete", "remove", "revoke"]

    for tool in tools.tools:
        has_destructive_keyword = any(
            keyword in tool.name.lower() for keyword in destructive_keywords
        )

        if has_destructive_keyword:
            assert tool.annotations is not None, f"Tool {tool.name} missing annotations"
            assert tool.annotations.destructiveHint is True, (
                f"Destructive tool {tool.name} should have destructiveHint=True"
            )


async def test_delete_operations_are_idempotent(nc_mcp_client: ClientSession):
    """Verify delete operations are marked as idempotent (ADR-017 decision)."""
    tools = await nc_mcp_client.list_tools()

    for tool in tools.tools:
        if "delete" in tool.name.lower():
            assert tool.annotations is not None, f"Tool {tool.name} missing annotations"
            assert tool.annotations.idempotentHint is True, (
                f"Delete tool {tool.name} should be idempotent (same end state)"
            )


async def test_create_operations_not_idempotent(nc_mcp_client: ClientSession):
    """Verify create operations are marked as non-idempotent."""
    tools = await nc_mcp_client.list_tools()

    for tool in tools.tools:
        if "create" in tool.name.lower() and "calendar_create_meeting" not in tool.name:
            assert tool.annotations is not None, f"Tool {tool.name} missing annotations"
            assert tool.annotations.idempotentHint is not True, (
                f"Create tool {tool.name} should not be idempotent (creates new resources)"
            )


async def test_update_operations_not_idempotent(nc_mcp_client: ClientSession):
    """Verify update operations are marked as non-idempotent (due to etag requirements)."""
    tools = await nc_mcp_client.list_tools()

    for tool in tools.tools:
        if "update" in tool.name.lower():
            assert tool.annotations is not None, f"Tool {tool.name} missing annotations"
            # Most updates use etags which change each time, making them non-idempotent
            # Exception: calendar_update_event might be different
            assert tool.annotations.idempotentHint is not True, (
                f"Update tool {tool.name} should not be idempotent (etag changes)"
            )


async def test_webdav_write_is_idempotent(nc_mcp_client: ClientSession):
    """Verify nc_webdav_write_file is marked as idempotent (ADR-017 decision).

    WebDAV write uses HTTP PUT without version control, making it idempotent.
    Writing same content to same path repeatedly produces same end state.
    """
    tools = await nc_mcp_client.list_tools()

    write_tool = next(
        (tool for tool in tools.tools if tool.name == "nc_webdav_write_file"), None
    )
    assert write_tool is not None, "nc_webdav_write_file tool not found"
    assert write_tool.annotations is not None, "write_file missing annotations"
    assert write_tool.annotations.idempotentHint is True, (
        "nc_webdav_write_file should be idempotent (HTTP PUT without version control)"
    )


async def test_semantic_search_open_world(nc_mcp_client: ClientSession):
    """Verify semantic search has openWorldHint=True (ADR-017 decision).

    Semantic search queries external Nextcloud service, consistent with other tools.
    """
    tools = await nc_mcp_client.list_tools()

    semantic_tool = next(
        (tool for tool in tools.tools if tool.name == "nc_semantic_search"), None
    )
    if semantic_tool:  # Only if semantic search is enabled
        assert semantic_tool.annotations is not None, (
            "semantic_search missing annotations"
        )
        assert semantic_tool.annotations.openWorldHint is True, (
            "nc_semantic_search should have openWorldHint=True (queries external service)"
        )


async def test_annotation_consistency(nc_mcp_client: ClientSession):
    """Verify annotation consistency across similar tools."""
    tools = await nc_mcp_client.list_tools()

    # Group tools by category
    categories = {
        "notes": [],
        "calendar": [],
        "contacts": [],
        "webdav": [],
        "tables": [],
        "deck": [],
        "cookbook": [],
        "sharing": [],
    }

    for tool in tools.tools:
        for category in categories:
            if tool.name.startswith(f"nc_{category}_"):
                categories[category].append(tool)

    # Within each category, similar operations should have similar annotations
    for category, category_tools in categories.items():
        # All list/search/get operations should be read-only
        read_ops = [
            t
            for t in category_tools
            if any(op in t.name for op in ["list", "search", "get"])
        ]
        for tool in read_ops:
            assert tool.annotations.readOnlyHint is True, (
                f"{tool.name} is a read operation but not marked read-only"
            )

        # All delete operations should be destructive and idempotent
        delete_ops = [t for t in category_tools if "delete" in t.name]
        for tool in delete_ops:
            assert tool.annotations.destructiveHint is True, (
                f"{tool.name} is a delete operation but not marked destructive"
            )
            assert tool.annotations.idempotentHint is True, (
                f"{tool.name} is a delete operation but not marked idempotent"
            )
