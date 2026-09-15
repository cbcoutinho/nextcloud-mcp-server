"""Module-level tool functions must not leak state between MCPServer instances.

Tools used to be closures rebuilt on every ``configure_*_tools(mcp)`` call, so
each server got its own function objects. They are now defined at module level
and registered with ``mcp.tool(...)(fn)``, which means every server shares the
*same* function objects.

That matters because ``configure_app_tools`` calls
``stamp_required_capability(tool.fn, ...)``, which **mutates the function**. It
is safe only because the stamp is idempotent and deterministic per app — it sets
the attribute once, and a tool belongs to exactly one app. This pins that
reasoning, since the failure mode is a server hiding tools based on another
server's capability gate rather than an exception.
"""

import pytest
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.capabilities import get_required_capability
from nextcloud_mcp_server.server import AVAILABLE_APPS, configure_app_tools

pytestmark = pytest.mark.unit


def _tools(app: str) -> list:
    mcp = MCPServer(f"srv-{app}")
    configure_app_tools(mcp, app)
    return mcp._tool_manager.list_tools()


def test_capability_stamps_do_not_leak_between_servers():
    """Two servers configured with different apps keep separate gates."""
    notes = _tools("notes")
    deck = _tools("deck")

    assert {get_required_capability(t.fn)[0] for t in notes} == {"notes"}
    assert {get_required_capability(t.fn)[0] for t in deck} == {"deck"}


def test_reconfiguring_the_same_app_is_stable():
    """Registering twice must not accumulate or rewrite gates.

    A non-idempotent stamp would show up here as a second server disagreeing
    with the first about the same tool.
    """
    first = {t.name: get_required_capability(t.fn) for t in _tools("deck")}
    second = {t.name: get_required_capability(t.fn) for t in _tools("deck")}

    assert first == second


def test_per_tool_capability_still_beats_the_module_stamp():
    """``@require_capability`` with a version floor must survive registration.

    ``deck_assign_dependent_card`` declares a Deck 1.18.0 floor; the whole-app
    stamp must not overwrite it with the unversioned gate.
    """
    gates = {t.name: get_required_capability(t.fn) for t in _tools("deck")}

    assert gates["deck_assign_dependent_card"] == ("deck", "1.18.0", None)


def test_every_app_registers_at_least_one_tool():
    """A registration call that silently no-ops would be invisible otherwise.

    The extraction rewrote every ``configure_*_tools`` body, and a dropped
    ``mcp.tool(...)(fn)`` line removes a tool from the wire without failing
    anything else.
    """
    for app in AVAILABLE_APPS:
        mcp = MCPServer("srv")
        configure_app_tools(mcp, app)
        assert mcp._tool_manager.list_tools(), f"{app} registered no tools"
