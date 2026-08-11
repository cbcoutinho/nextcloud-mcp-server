"""Guard: a per-session lifespan must not dispose process-lifetime storage.

``oauth_lifespan`` is entered and exited per MCP *session*, not per process --
a long-lived pod logs "Starting MCP server in OAuth mode" / "Shutting down MCP
server" many times a minute. Its ``finally`` used to call
``refresh_token_storage.close()``, but that object is built once at startup
(``setup_oauth_config``) and handed to the process-lifetime background tasks:
``token_storage`` in ``starlette_lifespan`` is the *same* instance that
``user_manager_task`` and ``credential_cleanup_task`` poll.

``close()`` sets ``engine = None``, so the first client session to end left the
supervisor asserting
``RefreshTokenStorage.initialize() not called`` once a minute forever, and a
newly provisioned user got no scanner until the pod was restarted -- observed
in production on a multi-user tenant.

The invariant is ownership, not the deletion: whoever *creates* a storage
closes it. ``app_lifespan_basic`` builds its own instance per session and so
may still close that one; it must never close ``app.state.storage``, which the
process lifespan owns.
"""

import ast
import inspect
from pathlib import Path

import pytest

import nextcloud_mcp_server.app as app_module

pytestmark = pytest.mark.unit

# Session-scoped lifespans. Neither may close storage it did not construct.
SESSION_LIFESPANS = {"oauth_lifespan", "app_lifespan_basic"}

# Storage objects owned elsewhere: built at startup or by the process lifespan.
FOREIGN_STORAGE = {"refresh_token_storage", "basic_auth_storage"}

# The same objects reached through app.state rather than by local name --
# starlette_lifespan publishes basic_auth_storage as app.state.storage, so
# `app.state.storage.close()` is the same mistake spelled differently.
FOREIGN_STORAGE_ATTRS = {"storage"}


def _closed_receiver(node: ast.Call) -> str | None:
    """Name of the object a ``<obj>.close()`` call disposes, else None.

    Matches a bare local (``refresh_token_storage.close()``) and an attribute
    chain ending in a known state slot (``app.state.storage.close()``). It is
    deliberately syntactic: an alias (``s = refresh_token_storage; s.close()``)
    slips past, since the point is to pin the invariant at the shapes the
    codebase actually uses, not to reimplement data-flow analysis.
    """
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "close"):
        return None
    receiver = node.func.value
    if isinstance(receiver, ast.Name) and receiver.id in FOREIGN_STORAGE:
        return receiver.id
    if isinstance(receiver, ast.Attribute) and receiver.attr in FOREIGN_STORAGE_ATTRS:
        return ast.unparse(receiver)
    return None


def _app_tree() -> ast.Module:
    return ast.parse(Path(inspect.getfile(app_module)).read_text())


def _lifespan_nodes() -> list[ast.AsyncFunctionDef]:
    """Every session-scoped lifespan, including ones nested in get_app()."""
    return [
        node
        for node in ast.walk(_app_tree())
        if isinstance(node, ast.AsyncFunctionDef) and node.name in SESSION_LIFESPANS
    ]


def test_session_lifespans_are_present():
    """Guard the guard: a rename would make the scan below vacuously pass."""
    found = {node.name for node in _lifespan_nodes()}
    assert found == SESSION_LIFESPANS, f"lifespans missing from app.py: {found}"


@pytest.mark.parametrize("lifespan", _lifespan_nodes(), ids=lambda node: node.name)
def test_session_lifespan_never_closes_foreign_storage(
    lifespan: ast.AsyncFunctionDef,
):
    """A session teardown must not dispose storage owned by the process."""
    offenders = [
        f"{receiver}.close() at app.py:{node.lineno}"
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call)
        and (receiver := _closed_receiver(node)) is not None
    ]
    assert not offenders, (
        f"{lifespan.name} is per-session but closes process-lifetime storage: "
        f"{offenders}. That nulls the engine the background user manager holds, "
        "so newly provisioned users get no scanner until the pod restarts."
    )
