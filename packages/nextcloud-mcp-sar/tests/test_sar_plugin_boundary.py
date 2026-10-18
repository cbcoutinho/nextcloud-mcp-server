"""SAR reaches the server only through ``nextcloud_mcp_server.plugin_api``.

Everything else in the server is internal and may change in any release, so an
import of it would tie this package to one server version.
"""

import ast
from importlib.util import resolve_name
from pathlib import Path

import nextcloud_mcp_sar
import pytest

pytestmark = pytest.mark.unit

SRC = Path(nextcloud_mcp_sar.__file__).parent
MODULES = sorted(SRC.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    package = ".".join(("nextcloud_mcp_sar", *path.relative_to(SRC).parts[:-1]))
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            # `from nextcloud_mcp_server import x` imports the submodule x.
            names = (
                [a.name for a in node.names] if module == "nextcloud_mcp_server" else []
            )
            found |= {f"{module}.{n}" for n in names} or {module}
    return found


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_imports_the_server_only_through_plugin_api(path):
    internal = {
        m
        for m in _imports(path)
        if m.split(".")[0] == "nextcloud_mcp_server"
        and m != "nextcloud_mcp_server.plugin_api"
    }
    assert not internal, (
        f"{path.name} imports server internals {sorted(internal)}: add what it "
        "needs to nextcloud_mcp_server.plugin_api instead"
    )
