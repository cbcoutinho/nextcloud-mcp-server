"""LLM-friendly rendering of tool failures (GH #1208).

The MCP SDK funnels every unanticipated tool exception into a ``ToolError`` and
returns its message as the error content. On mcp 1.x that message was
``f"Error executing tool {name}: {e}"``, so an unhandled ``httpx`` error reached
the model as an internal URL plus an MDN link and no hint about what to do next.
mcp 2.x withholds the original entirely -- ``UnexpectedToolError`` says only
``Error executing tool {name}`` -- which is less misleading and even less
actionable.

Either way the original is on ``__cause__``, so rewriting that one message at
the tool boundary fixes every tool at once -- see :class:`NextcloudMCPServer`.
"""

import json
import re
from typing import Any
from urllib.parse import unquote

from httpx import URL, HTTPStatusError, RequestError, Response, ResponseNotRead
from mcp.server.context import CallNext, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, InputRequiredResult
from mcp.types import Tool as MCPTool

from nextcloud_mcp_server.capabilities import (
    enforce_capability,
    filter_by_capability,
)
from nextcloud_mcp_server.request_context import (
    reset_request_context,
    set_request_context,
)
from nextcloud_mcp_server.serialization import compact_tool_result

#: ``summary``/``hint`` per status. 429 is rare -- ``retry_on_429`` in
#: ``client/base.py`` absorbs it -- but ``_stream_request`` re-raises it once
#: its own retries are exhausted, so it still needs a hint that says "back off"
#: rather than the generic "re-check the arguments".
_STATUS: dict[int, tuple[str, str]] = {
    400: (
        "Invalid request",
        "Check the argument values -- one of them was rejected as malformed.",
    ),
    401: (
        "Authentication failed",
        "The Nextcloud credentials or session are invalid or expired; "
        "re-authenticate before retrying.",
    ),
    403: (
        "Permission denied",
        "The account cannot access this resource, or the required scope was "
        "not granted. Do not retry without different credentials or scopes.",
    ),
    404: (
        "Not found",
        "Check the path or id for typos and verify the resource exists; "
        "list the parent first if unsure.",
    ),
    405: (
        "Operation not allowed here",
        "The resource may already exist, or this operation is unsupported at "
        "that location.",
    ),
    409: (
        "Conflict",
        "A parent folder may be missing, or a resource with that name already "
        "exists. Create the parent or pick another name.",
    ),
    412: (
        "Resource changed since it was read",
        "Re-read the resource to get its current etag, then retry the update "
        "with that etag.",
    ),
    413: (
        "Payload too large",
        "The upload exceeds the server limit; split it or use a smaller file.",
    ),
    423: (
        "Resource locked",
        "Another process holds a lock on it. Retry shortly, or report the lock "
        "to the user.",
    ),
    429: (
        "Rate limited",
        "Nextcloud is throttling requests and the client already retried and "
        "gave up. Wait before retrying -- do not rewrite the arguments.",
    ),
    507: (
        "Insufficient storage",
        "The account's quota is exhausted; free space before retrying.",
    ),
}

_GENERIC_CLIENT = (
    "Request rejected",
    "The server refused the request as sent; re-check the arguments.",
)
_GENERIC_SERVER = (
    "Nextcloud server error",
    "This is usually transient -- retry once, and report it to the user if it "
    "persists.",
)

#: DAV (``<s:message>``) and OCS (``<message>``) both put the reason here.
_XML_MESSAGE = re.compile(r"<(?:\w+:)?message[^>]*>(.*?)</(?:\w+:)?message>", re.S)

_MAX_DETAIL = 200


def _resource(url: URL) -> str:
    """The user-meaningful part of a request path, without internal routing.

    Strips the entry points ``_resolve_url`` and the DAV clients prepend, so
    ``/remote.php/dav/files/admin/FileUpload/test.txt`` renders as
    ``FileUpload/test.txt`` and ``/index.php/apps/notes/api/v1/notes/5`` as
    ``apps/notes/api/v1/notes/5``.
    """
    path = unquote(url.path)
    if path.startswith("/remote.php/dav/files/"):
        # /remote.php/dav/files/<principal>/<rest>; the home root itself has no
        # <rest>, so fall through to the full path rather than quoting "".
        parts = path.split("/", 5)
        if len(parts) > 5 and parts[5]:
            return parts[5]
    for prefix in ("/index.php", "/ocs/v1.php", "/ocs/v2.php", "/remote.php"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    return path.lstrip("/")


def _server_detail(response: Response) -> str:
    """The reason Nextcloud itself gave, when it published a usable one."""
    try:
        body = response.text
    except ResponseNotRead:
        # Streaming downloads raise with the body still unread; there is
        # nothing to parse and reading it here would defeat the streaming.
        return ""

    body = body.strip()
    if not body or body[:20].lower().startswith(("<!doctype", "<html")):
        return ""

    candidate = None
    try:
        payload = json.loads(body)
    except ValueError:
        match = _XML_MESSAGE.search(body)
        candidate = match.group(1) if match else None
    else:
        if isinstance(payload, dict):
            meta = payload.get("ocs")
            meta = meta.get("meta") if isinstance(meta, dict) else None
            candidate = (
                (meta.get("message") if isinstance(meta, dict) else None)
                or payload.get("message")
                or payload.get("error")
            )

    message = " ".join(candidate.split()) if isinstance(candidate, str) else ""
    if not message:
        return ""
    if len(message) > _MAX_DETAIL:
        message = message[: _MAX_DETAIL - 1].rstrip() + "…"
    return f" Server said: {message}"


def friendly_tool_error(exc: BaseException | None, tool_name: str) -> str | None:
    """Render ``exc`` for an LLM, or ``None`` if it is better left alone.

    ``None`` means "no improvement to offer" -- the caller re-raises unchanged,
    so tools that already build a tailored ``MCPError`` keep their own wording.
    """
    if isinstance(exc, HTTPStatusError):
        status = exc.response.status_code
        summary, hint = _STATUS.get(
            status, _GENERIC_SERVER if status >= 500 else _GENERIC_CLIENT
        )
        # exc.request, not exc.response.url: a few call sites raise with a
        # synthetic Response that has no request bound, where .url explodes.
        return (
            f'{tool_name} failed: {summary} — "{_resource(exc.request.url)}" '
            f"(HTTP {status}). {hint}{_server_detail(exc.response)}"
        )

    if isinstance(exc, RequestError):
        return (
            f"{tool_name} failed: could not reach Nextcloud ({exc.__class__.__name__}). "
            "The server may be down or unreachable from here; retry once, then "
            "report it to the user."
        )

    return None


class NextcloudMCPServer(MCPServer):
    """MCPServer that rewrites raw HTTP failures into LLM-friendly messages,
    hides tools this Nextcloud instance cannot serve, and strips the SDK's
    ``indent=2`` from tool results (GH #1395).

    Subclass rather than patch: ``MCPServer._setup_handlers`` binds
    ``self.call_tool``/``self.list_tools`` during ``__init__``, so a later
    reassignment is ignored.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.middleware.append(self._publish_request_context)

    async def _publish_request_context(
        self, ctx: ServerRequestContext, call_next: CallNext
    ) -> Any:
        """Republish ``ctx`` for the handlers mcp 2.x cannot inject it into.

        ``list_tools()`` (capability gating) and static ``@mcp.resource()``
        functions get no ``Context`` from the SDK and have no contextvar to read
        since 2.x dropped ``request_ctx``; a middleware is where 2.x puts
        per-message interception. See ``nextcloud_mcp_server.request_context``.
        """
        token = set_request_context(ctx)
        try:
            return await call_next(ctx)
        finally:
            reset_request_context(token)

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        return await filter_by_capability(self, tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        await enforce_capability(self, name)
        try:
            return compact_tool_result(
                await super().call_tool(name, arguments, context)
            )
        except ToolError as e:
            message = friendly_tool_error(e.__cause__, name)
            if message is None:
                raise
            raise ToolError(message) from e.__cause__
        except MCPError as e:
            # mcp 2.x passes MCPError out of a tool as a top-level JSON-RPC
            # error; 1.x turned it into ``is_error=True`` content the model
            # reads and reacts to. Every one of our raise sites is that second
            # kind ("Note 5 not found", "Nextcloud access not provisioned"),
            # which in 2.x is what ToolError means -- so map it back rather
            # than silently change the wire contract for ~140 messages.
            raise ToolError(e.message) from e
