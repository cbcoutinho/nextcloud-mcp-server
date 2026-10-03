"""Plugin loading via the ``nextcloud_mcp_server.plugins`` entry-point group."""

from importlib.metadata import EntryPoint
from types import SimpleNamespace

import pytest

from nextcloud_mcp_server import plugins
from nextcloud_mcp_server.models.auth import SAR_SCOPES
from nextcloud_mcp_server.plugins import Plugin, load_plugins, register_plugin_tools


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
    sar = {p.name: p for p in load_plugins()}["sar"]
    assert sar.scopes == SAR_SCOPES


def test_entry_point_must_name_a_plugin(monkeypatch):
    _install(monkeypatch, bogus="nextcloud_mcp_server.features:sar_available")
    with pytest.raises(TypeError, match="bogus"):
        load_plugins()


def test_plugin_names_must_be_unique(monkeypatch):
    _install(
        monkeypatch,
        a="nextcloud_mcp_server.sar_plugin:plugin",
        b="nextcloud_mcp_server.sar_plugin:plugin",
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
