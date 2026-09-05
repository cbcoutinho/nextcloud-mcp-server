"""The per-request MCP ``Context``, for code the SDK cannot inject it into.

mcp 2.x injects ``Context`` into tool functions and *template*-resource
functions, and removed both the ``mcp.server.lowlevel.server.request_ctx``
contextvar and ``MCPServer.get_context()``. That leaves three callers here with
no route to a context at all:

* ``MCPServer.list_tools()`` -- capability gating needs an authenticated client
  to ask the instance what it can serve, and the override takes no context.
* static ``@mcp.resource()`` functions (``nc://capabilities``,
  ``notes://settings``, ``cookbook://version``) -- registering one with a ``ctx``
  parameter is refused outright by the SDK.
* ``capabilities.enforce_capability`` on the ``tools/call`` path.

:class:`~nextcloud_mcp_server.errors.NextcloudMCPServer` republishes it here from
a middleware, which is where mcp 2.x puts per-message interception. Handlers the
SDK *does* inject into must keep taking ``ctx`` as a parameter -- this is the
fallback for the ones it doesn't, not a general substitute.
"""

from contextvars import ContextVar, Token
from typing import Any

from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer

_request_ctx: ContextVar[ServerRequestContext] = ContextVar("nextcloud_request_ctx")


def set_request_context(ctx: ServerRequestContext) -> Token[ServerRequestContext]:
    """Publish ``ctx`` for the message being served. Returns a reset token."""
    return _request_ctx.set(ctx)


def reset_request_context(token: Token[ServerRequestContext]) -> None:
    _request_ctx.reset(token)


def current_context(mcp: MCPServer | None = None) -> Context[Any, Any]:
    """The high-level ``Context`` for the MCP message currently being served.

    Raises:
        LookupError: outside a request served through the middleware -- notably
            a direct ``mcp.call_tool()`` in a unit test, where callers fail open.
    """
    return Context(request_context=_request_ctx.get(), mcp_server=mcp)
