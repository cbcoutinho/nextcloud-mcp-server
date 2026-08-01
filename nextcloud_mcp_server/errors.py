"""LLM-friendly rendering of tool failures (GH #1208).

The MCP SDK funnels every tool exception through
``ToolError(f"Error executing tool {name}: {e}")`` and returns that string
verbatim as the error content, so an unhandled ``httpx`` error reaches the model
as an internal URL plus an MDN link and no hint about what to do next. Rewriting
that one string at the tool boundary fixes every tool at once -- see
:class:`NextcloudFastMCP`.
"""

import json
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import unquote

from httpx import URL, HTTPStatusError, RequestError, Response, ResponseNotRead
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ContentBlock

#: ``summary``/``hint`` per status. 429 is absent on purpose: ``retry_on_429``
#: in ``client/base.py`` absorbs it before it can reach a tool.
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
    so tools that already build a tailored ``McpError`` keep their own wording.
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


class NextcloudFastMCP(FastMCP):
    """FastMCP that rewrites raw HTTP failures into LLM-friendly messages.

    Subclass rather than patch: ``FastMCP._setup_handlers`` binds
    ``self.call_tool`` during ``__init__``, so a later reassignment is ignored.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as e:
            message = friendly_tool_error(e.__cause__, name)
            if message is None:
                raise
            raise ToolError(message) from e.__cause__
