"""Unit tests for the stdio transport module."""

import pytest
from mcp.server.fastmcp import FastMCP

from nextcloud_mcp_server.config import _reload_config
from nextcloud_mcp_server.config_validators import AuthMode
from nextcloud_mcp_server.stdio import get_stdio_mcp


@pytest.fixture
def single_user_env(monkeypatch):
    """Set up environment variables for single-user BasicAuth mode."""
    monkeypatch.setenv("NEXTCLOUD_HOST", "https://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "admin")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "secret")
    # Ensure multi-user mode is off (may leak from other tests)
    monkeypatch.delenv("ENABLE_MULTI_USER_BASIC_AUTH", raising=False)
    _reload_config()
    yield
    _reload_config()


@pytest.mark.unit
def test_get_stdio_mcp_returns_fastmcp(single_user_env):
    """get_stdio_mcp returns a FastMCP instance with correct env vars."""
    mcp = get_stdio_mcp()
    assert isinstance(mcp, FastMCP)


@pytest.mark.unit
def test_get_stdio_mcp_rejects_config_errors(monkeypatch):
    """get_stdio_mcp raises ValueError when validate_configuration reports errors."""
    monkeypatch.setattr(
        "nextcloud_mcp_server.stdio.validate_configuration",
        lambda _settings: (AuthMode.SINGLE_USER_BASIC, ["missing nextcloud_host"]),
    )
    with pytest.raises(ValueError, match="Configuration validation failed"):
        get_stdio_mcp()


@pytest.mark.unit
def test_get_stdio_mcp_rejects_non_single_user_mode(monkeypatch):
    """get_stdio_mcp raises ValueError for non-single-user modes."""
    monkeypatch.setattr(
        "nextcloud_mcp_server.stdio.validate_configuration",
        lambda _settings: (AuthMode.MULTI_USER_BASIC, []),
    )
    with pytest.raises(ValueError, match="single-user BasicAuth"):
        get_stdio_mcp()


@pytest.mark.unit
def test_get_stdio_mcp_registers_all_apps_by_default(single_user_env):
    """All app tool groups are registered when no filter is specified."""
    mcp = get_stdio_mcp()
    # NOTE: _tool_manager is a FastMCP internal; may break on SDK upgrades
    tools = mcp._tool_manager.list_tools()
    tool_names = {t.name for t in tools}

    # Spot-check representative tools from each app
    assert "nc_notes_get_note" in tool_names
    assert "nc_webdav_list_directory" in tool_names
    assert "nc_calendar_list_calendars" in tool_names
    assert "nc_contacts_list_addressbooks" in tool_names
    assert "nc_cookbook_list_recipes" in tool_names
    assert "deck_get_boards" in tool_names
    assert "nc_tables_list_tables" in tool_names
    assert "nc_share_list" in tool_names
    assert "nc_news_list_feeds" in tool_names
    assert "collectives_get_collectives" in tool_names


@pytest.mark.unit
def test_get_stdio_mcp_respects_enabled_apps(single_user_env):
    """Only specified apps have their tools registered."""
    mcp = get_stdio_mcp(enabled_apps=["notes"])
    # NOTE: _tool_manager is a FastMCP internal; may break on SDK upgrades
    tools = mcp._tool_manager.list_tools()
    tool_names = {t.name for t in tools}

    assert "nc_notes_get_note" in tool_names
    # Other apps should NOT be present
    assert "nc_webdav_list_directory" not in tool_names
    assert "nc_calendar_list_calendars" not in tool_names


@pytest.mark.unit
def test_get_stdio_mcp_no_semantic_tools(single_user_env):
    """Semantic search tools are never registered in stdio mode."""
    mcp = get_stdio_mcp()
    # NOTE: _tool_manager is a FastMCP internal; may break on SDK upgrades
    tools = mcp._tool_manager.list_tools()
    tool_names = {t.name for t in tools}

    semantic_names = [n for n in tool_names if "semantic" in n or "vector" in n]
    assert semantic_names == [], f"Unexpected semantic tools: {semantic_names}"


@pytest.mark.unit
def test_get_stdio_mcp_registers_capabilities_resource(single_user_env):
    """The nc://capabilities resource is registered."""
    mcp = get_stdio_mcp()
    # NOTE: _resource_manager._resources is a FastMCP internal; may break on SDK upgrades
    resources = mcp._resource_manager._resources
    assert "nc://capabilities" in resources
