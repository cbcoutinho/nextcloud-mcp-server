"""The plugin API is the only way into the server for a plugin.

SAR is the in-tree plugin that has to stay extractable into its own
distribution (Deck #1386): it may import its own modules and
``nextcloud_mcp_server.plugin_api``, nothing else from the server.
"""

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

from nextcloud_mcp_server import plugin_api

pytestmark = pytest.mark.unit

PACKAGE = Path(plugin_api.__file__).parent

SAR_MODULES = {
    "nextcloud_mcp_server.api.sar",
    "nextcloud_mcp_server.models.sar",
    "nextcloud_mcp_server.providers.ner",
    "nextcloud_mcp_server.redaction",
    "nextcloud_mcp_server.sar_case",
    "nextcloud_mcp_server.sar_export",
    "nextcloud_mcp_server.sar_plugin",
    "nextcloud_mcp_server.server.sar",
}


def _imports(module: str) -> set[str]:
    path = PACKAGE.joinpath(*module.split(".")[1:]).with_suffix(".py")
    package = module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(resolve_name("." * node.level + (node.module or ""), package))
    return found


@pytest.mark.parametrize("module", sorted(SAR_MODULES))
def test_sar_reaches_the_server_only_through_plugin_api(module):
    allowed = SAR_MODULES | {"nextcloud_mcp_server.plugin_api"}
    internal = {
        m
        for m in _imports(module)
        if m.split(".")[0] == "nextcloud_mcp_server" and m not in allowed
    }
    assert not internal, (
        f"{module} imports server internals {sorted(internal)}: add what it "
        "needs to nextcloud_mcp_server.plugin_api instead"
    )


@pytest.mark.parametrize("name", plugin_api.__all__)
def test_every_plugin_api_name_resolves(name):
    assert getattr(plugin_api, name) is not None


def test_unknown_names_raise_attribute_error():
    with pytest.raises(AttributeError, match="no_such_name"):
        plugin_api.no_such_name  # noqa: B018
