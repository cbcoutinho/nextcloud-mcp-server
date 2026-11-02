"""GHSA-29mh-jqx8-rg3v: MCP resources must enforce the same scopes as tools.

Every ``@mcp.resource`` reading app data used to carry no ``@require_scopes``,
and the decorator could not have gated the *static* ones anyway: it treated
"no Context parameter" as BasicAuth-allow, and the SDK refuses a ``ctx``
parameter on static resources. A token holding only ``openid`` could
``resources/read`` notes, deck and cookbook data its tools refuse.
"""

from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.auth.scope_authorization import (
    InsufficientScopeError,
    require_scopes,
)
from nextcloud_mcp_server.config_validators import AuthMode
from nextcloud_mcp_server.request_context import (
    reset_request_context,
    set_request_context,
)
from nextcloud_mcp_server.stdio import get_stdio_mcp
from tests.unit.test_require_scopes_enforcement import (
    _authenticated_with,
    _request_context,
    _settings,
)

pytestmark = pytest.mark.unit


def _server() -> MCPServer:
    """The full stdio server: every app plus nc://capabilities, which the
    server factories register themselves (app.py mirrors stdio.py)."""
    with patch(
        "nextcloud_mcp_server.stdio.validate_configuration",
        return_value=(AuthMode.SINGLE_USER_BASIC, []),
    ):
        return get_stdio_mcp()


def test_every_resource_requires_a_scope():
    """Guard: a new resource is unguarded by default unless this fails."""
    rm = _server()._resource_manager
    unguarded = [
        str(r.uri)
        for r in rm.list_resources()
        if not getattr(r.fn, "_required_scopes", None)
    ] + [
        t.uri_template
        for t in rm.list_templates()
        if not getattr(t.fn, "_required_scopes", None)
    ]
    assert unguarded == []


@pytest.mark.parametrize(
    "uri",
    ["notes://settings", "nc://Notes/1", "nc://Deck/boards", "cookbook://config"],
)
async def test_resource_read_refused_without_app_scope(uri):
    """Static and template resources alike refuse an openid-only token."""
    mcp = _server()
    get_client = AsyncMock(side_effect=AssertionError("reached Nextcloud"))
    reset = set_request_context(_request_context())
    try:
        with (
            _settings(),
            _authenticated_with(["openid"]),
            patch("nextcloud_mcp_server.server.notes.get_client", get_client),
            patch("nextcloud_mcp_server.server.deck.get_client", get_client),
            patch("nextcloud_mcp_server.server.cookbook.get_client", get_client),
        ):
            with pytest.raises(Exception) as exc_info:
                await mcp.read_resource(uri)
    finally:
        reset_request_context(reset)

    # The SDK wraps non-MCPError handler exceptions in UnexpectedResourceError,
    # keeping the original as __cause__.
    err = exc_info.value
    assert isinstance(err, InsufficientScopeError) or isinstance(
        err.__cause__, InsufficientScopeError
    )
    get_client.assert_not_called()


@require_scopes("notes.read")
async def _no_ctx_handler() -> str:
    return "executed"


async def test_no_context_denied_under_oauth():
    """No injected ctx and no request context must not mean 'allow' under OAuth."""
    with _settings(login_flow=True), _authenticated_with(["openid", "notes.read"]):
        with pytest.raises(InsufficientScopeError):
            await _no_ctx_handler()


async def test_no_context_allowed_under_basicauth():
    with _settings(login_flow=False):
        assert await _no_ctx_handler() == "executed"
