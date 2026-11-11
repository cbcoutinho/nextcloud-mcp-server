"""Guard: the Nextcloud clients import nothing from the MCP server.

``nextcloud_client`` ships as the standalone ``nextcloud-client`` package, and
its declared dependencies do not include the server or the server's
libraries. In the dev environment both are installed, so a stray
``import nextcloud_mcp_server...`` or ``import prometheus_client`` in a client
module would pass every other test and only fail for PyPI users. This test
imports every client module in a fresh interpreter where those imports raise.
"""

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

#: The server itself, and third-party packages that only the server needs.
SERVER_ONLY = (
    "nextcloud_mcp_server",
    "mcp",
    "dynaconf",
    "prometheus_client",
    "opentelemetry",
    "sqlalchemy",
    "alembic",
    "starlette",
    "uvicorn",
    "authlib",
    "langchain_text_splitters",
)

PROBE = textwrap.dedent(
    """
    import sys

    SERVER_ONLY = {server_only!r}

    class Block:
        def find_spec(self, name, path=None, target=None):
            if name.partition(".")[0] in SERVER_ONLY:
                raise ImportError(f"client imported server-only module {{name}}")
            return None

    sys.meta_path.insert(0, Block())

    import pkgutil
    import importlib
    import nextcloud_client as pkg

    for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        importlib.import_module(mod.name)
    print("ok")
    """
)


def test_client_package_imports_without_server_modules():
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(server_only=SERVER_ONLY)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_server_installs_metric_hooks():
    """Importing the server's metrics module routes client hooks to prometheus."""
    from nextcloud_client import instrumentation

    from nextcloud_mcp_server.observability import metrics

    counter = metrics.nextcloud_api_requests_total.labels(
        app="boundary-test", method="GET", status_code="200"
    )
    before = counter._value.get()
    instrumentation.hooks.on_api_call(
        app="boundary-test", method="GET", status_code=200, duration=0.01
    )
    assert counter._value.get() == before + 1


@pytest.mark.parametrize(
    "entrypoint",
    [
        "nextcloud_mcp_server.cli",  # the console script (HTTP server, workers)
        "nextcloud_mcp_server.app",
        "nextcloud_mcp_server.stdio",
    ],
)
def test_entrypoints_install_server_hooks(entrypoint):
    """The server hooks are installed as an import side effect of
    ``observability.metrics``. An entrypoint that stopped importing it would
    build clients that drop every metric and span with no warning.
    """
    probe = textwrap.dedent(
        f"""
        import importlib
        importlib.import_module({entrypoint!r})
        from nextcloud_mcp_server.client import instrumentation
        print(type(instrumentation.hooks).__name__)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "_ServerInstrumentation"


def test_probe_catches_a_leak():
    """Guard the guard: the blocker must actually reject a server import."""
    leaky = PROBE.format(server_only=SERVER_ONLY).replace(
        'print("ok")', "import nextcloud_mcp_server.config"
    )
    result = subprocess.run(
        [sys.executable, "-c", leaky], capture_output=True, text=True, timeout=120
    )
    assert result.returncode != 0
    assert "server-only module nextcloud_mcp_server" in result.stderr
