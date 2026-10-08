"""Plugin loading via the ``nextcloud_mcp_server.plugins`` entry-point group."""

from importlib.metadata import EntryPoint
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server import plugins
from nextcloud_mcp_server.models.auth import CORE_SCOPES
from nextcloud_mcp_server.plugins import (
    Plugin,
    check_plugins,
    load_plugins,
    register_plugin_tools,
    supported_scopes,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_plugin_cache():
    load_plugins.cache_clear()
    yield
    load_plugins.cache_clear()


def _install(monkeypatch, **targets: str) -> None:
    """Pretend exactly these entry points are installed."""
    eps = [
        EntryPoint(name=name, value=value, group=plugins.ENTRY_POINT_GROUP)
        for name, value in targets.items()
    ]
    monkeypatch.setattr(plugins, "entry_points", lambda group: eps)


def test_sar_is_registered_through_the_entry_point():
    """The packaging metadata, not an import in app.py, is what wires SAR in."""
    installed = {p.name: p for p in load_plugins()}
    assert "sar" in installed, (
        "no 'sar' plugin entry point: the installed package metadata predates "
        "the entry point -- reinstall the project (uv sync)"
    )
    assert installed["sar"].scopes == {"sar.read", "sar.write"}


def test_entry_point_must_name_a_plugin(monkeypatch):
    _install(monkeypatch, bogus="nextcloud_mcp_sar.plugin:sar_available")
    with pytest.raises(TypeError, match="bogus"):
        load_plugins()


def test_plugin_names_must_be_unique(monkeypatch):
    _install(
        monkeypatch,
        a="nextcloud_mcp_sar.plugin:plugin",
        b="nextcloud_mcp_sar.plugin:plugin",
    )
    with pytest.raises(ValueError, match="sar"):
        load_plugins()


def test_only_available_plugins_register_tools(monkeypatch):
    registered: list[str] = []

    def make(name: str, available: bool) -> Plugin:
        return Plugin(
            name=name,
            available=lambda settings: available,
            register_tools=lambda mcp: registered.append(name),
        )

    monkeypatch.setattr(
        plugins, "load_plugins", lambda: (make("on", True), make("off", False))
    )

    register_plugin_tools(SimpleNamespace(), settings=None)  # ty: ignore[invalid-argument-type]

    assert registered == ["on"]


@pytest.mark.parametrize("name", ["rerank", "Bad-Name", "1sar", ""])
def test_plugin_name_must_be_a_safe_status_key(monkeypatch, name):
    """The name becomes ``<name>_available`` on /api/v1/status."""
    bad = Plugin(name=name, available=lambda s: True, register_tools=lambda m: None)
    monkeypatch.setattr(plugins, "_TEST_PLUGIN", bad, raising=False)
    _install(monkeypatch, bad="nextcloud_mcp_server.plugins:_TEST_PLUGIN")
    with pytest.raises(ValueError, match="invalid plugin name"):
        load_plugins()


def test_load_failure_names_the_entry_point(monkeypatch):
    _install(monkeypatch, broken="nextcloud_mcp_server.no_such_module:plugin")
    with pytest.raises(RuntimeError, match="broken"):
        load_plugins()


def _with_scopes(name: str, *scopes: str) -> Plugin:
    return Plugin(
        name=name,
        available=lambda s: True,
        register_tools=lambda m: None,
        scopes=frozenset(scopes),
    )


def test_plugin_scopes_join_the_vocabulary(monkeypatch):
    """A plugin's own scopes become grantable without editing core."""
    monkeypatch.setattr(
        plugins, "_TEST_PLUGIN", _with_scopes("extra", "extra.read"), raising=False
    )
    _install(monkeypatch, extra="nextcloud_mcp_server.plugins:_TEST_PLUGIN")
    assert supported_scopes() == CORE_SCOPES | {"extra.read"}


@pytest.mark.parametrize("scope", ["notes.read", "Extra.read", "extra", "x.y.z"])
def test_plugin_scopes_must_be_well_formed_and_its_own(monkeypatch, scope):
    """Withholding a plugin's scopes from DCR must never withhold a core one."""
    monkeypatch.setattr(
        plugins, "_TEST_PLUGIN", _with_scopes("extra", scope), raising=False
    )
    _install(monkeypatch, extra="nextcloud_mcp_server.plugins:_TEST_PLUGIN")
    with pytest.raises(ValueError, match="malformed or already taken"):
        load_plugins()


def test_two_plugins_cannot_share_a_scope(monkeypatch):
    monkeypatch.setattr(plugins, "_A", _with_scopes("a", "x.read"), raising=False)
    monkeypatch.setattr(plugins, "_B", _with_scopes("b", "x.read"), raising=False)
    _install(
        monkeypatch,
        a="nextcloud_mcp_server.plugins:_A",
        b="nextcloud_mcp_server.plugins:_B",
    )
    with pytest.raises(ValueError, match="'b'"):
        load_plugins()


def test_every_plugin_tool_scope_is_grantable():
    """test_every_tool_scope_is_grantable, for the installed plugins' tools."""
    for plugin in load_plugins():
        mcp = MCPServer(name=f"test-{plugin.name}")
        plugin.register_tools(mcp)
        required = {
            scope
            for tool in mcp._tool_manager.list_tools()
            for scope in getattr(tool.fn, "_required_scopes", ())
        }
        assert required <= supported_scopes(), (
            f"{plugin.name}: not grantable: {sorted(required - supported_scopes())}"
        )


def test_a_misconfigured_plugin_fails_startup_by_name(monkeypatch):
    def misconfigured(settings):
        raise ValueError("FAKE_ENABLED requires FAKE_URL")

    broken = Plugin(name="fake", available=misconfigured, register_tools=lambda m: None)
    monkeypatch.setattr(plugins, "load_plugins", lambda: (broken,))
    with pytest.raises(RuntimeError, match="'fake' failed to check its settings"):
        check_plugins(settings=None)  # ty: ignore[invalid-argument-type]


def test_a_failing_plugin_is_named_at_registration(monkeypatch):
    """A plugin's own register_tools/routes failure names the plugin, like an
    entry point that fails to load, rather than a bare traceback."""

    def boom(*_):
        raise KeyError("missing setting")

    broken = Plugin(
        name="broken",
        available=lambda settings: True,
        register_tools=boom,
        routes=boom,
    )
    monkeypatch.setattr(plugins, "load_plugins", lambda: (broken,))

    with pytest.raises(RuntimeError, match="'broken' failed to register its tools"):
        register_plugin_tools(SimpleNamespace(), settings=None)  # ty: ignore[invalid-argument-type]
    with pytest.raises(RuntimeError, match="'broken' failed to build its routes"):
        plugins.plugin_routes(settings=None)  # ty: ignore[invalid-argument-type]
