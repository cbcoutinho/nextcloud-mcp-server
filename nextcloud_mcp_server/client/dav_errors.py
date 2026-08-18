"""Surface the explanation Nextcloud's DAV layer already sends on a failure.

Sabre/DAV answers a failed request with a document naming the concrete cause::

    <?xml version="1.0" encoding="utf-8"?>
    <d:error xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns">
      <s:exception>Sabre\\DAV\\Exception\\Locked</s:exception>
      <s:message>File is currently write locked</s:message>
    </d:error>

The status alone under-specifies the cause: a bare ``412`` says nothing about
*which* precondition failed, and a ``403`` can be a share permission, a blocked
filename, or a quota rule. Discarding that body is the difference between an
actionable error and a shrug -- an unfiltered WebDAV SEARCH returning ``500``
took far longer to diagnose than it should have, because the server was saying
``TypeError`` in a body nobody read.

Three statuses get their own type because callers branch on them:

===  ==============================  =======================================
412  :class:`DavPreconditionFailed`  ``If-Match`` ETag no longer current
423  :class:`DavLocked`              resource write-locked by another client
507  :class:`DavInsufficientStorage` quota exhausted
===  ==============================  =======================================

Everything here derives from :class:`httpx.HTTPStatusError`, so handlers that
predate this module keep catching these unchanged.

Both of ``BaseNextcloudClient``'s request paths are wired to this: the buffered
``_make_request`` and the streaming ``_stream_request``. The streaming one
raises before its body is read, so it gets the status-based type without the
server's wording -- a 507 download is still a
:class:`DavInsufficientStorage`.
"""

import xml.etree.ElementTree as ET

from httpx import HTTPStatusError, Request, Response, ResponseNotRead

# An XML namespace URI, not an address anything is fetched from. Namespaces are
# matched as exact strings, so "upgrading" this to https would stop every Sabre
# error document from being recognised. The same literal appears inline in
# webdav.py's PROPFIND bodies.
SABREDAV_NAMESPACE = "http://sabredav.org/ns"  # NOSONAR(S5332)

# DAV error documents run to a few hundred bytes. Anything larger is not one,
# and handing it to the XML parser would turn a failed request into an
# unbounded amount of work on a path that is already failing.
MAX_ERROR_BODY_BYTES = 64 * 1024


class DavError(HTTPStatusError):
    """A failed DAV request, carrying the server's own explanation."""

    def __init__(
        self,
        message: str,
        *,
        request: Request,
        response: Response,
        dav_exception: str | None = None,
        dav_message: str | None = None,
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.dav_exception = dav_exception
        self.dav_message = dav_message


class DavPreconditionFailed(DavError):
    """412 -- a precondition failed, typically a stale ``If-Match`` ETag."""


class DavLocked(DavError):
    """423 -- the resource is write-locked by another client."""


class DavInsufficientStorage(DavError):
    """507 -- the write would exceed the available quota."""


_STATUS_TYPES: dict[int, type[DavError]] = {
    412: DavPreconditionFailed,
    423: DavLocked,
    507: DavInsufficientStorage,
}


def parse_dav_error_body(response: Response) -> tuple[str | None, str | None]:
    """Extract ``(s:exception, s:message)`` from a DAV error response.

    Returns ``(None, None)`` for anything that is not a readable Sabre error
    document -- an unread streaming response, a JSON body, an oversized body,
    or malformed XML. This runs while another error is already being raised, so
    it must never raise one of its own.
    """
    try:
        body = response.content
    except ResponseNotRead:
        # Streaming responses raise before the body is read. Nothing to add.
        return None, None

    # Anything that is not a real byte body -- a test double, a stub response
    # from a caller that fakes the httpx surface -- is not a DAV error document.
    # This runs while another exception is already propagating, so degrading to
    # "no detail" is the only acceptable outcome. Raising here would replace the
    # caller's real failure with a TypeError from the error handler itself.
    if not isinstance(body, (bytes, bytearray)):
        return None, None

    if not body or len(body) > MAX_ERROR_BODY_BYTES:
        return None, None

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        # Not XML (an OCS JSON body, an HTML error page from a proxy) or
        # truncated. Both are ordinary here, not worth a log line.
        return None, None

    exception = root.find(f".//{{{SABREDAV_NAMESPACE}}}exception")
    message = root.find(f".//{{{SABREDAV_NAMESPACE}}}message")
    return (
        exception.text if exception is not None else None,
        message.text if message is not None else None,
    )


def dav_error_from_response(
    status: int, *, method: str, url: str, body: bytes | str = b""
) -> HTTPStatusError:
    """Build the typed error for a DAV failure observed outside httpx.

    The CalDAV client talks through the ``caldav`` library rather than
    ``BaseNextcloudClient._make_request`` (an intentional exception -- it has
    its own DAV session), and ``caldav``'s ``put`` *returns* a 412 rather than
    raising. So that path has a status, a URL and a body but no
    ``HTTPStatusError`` to enrich. This wraps them into the same vocabulary, so
    a stale-ETag CalDAV write raises the same
    :class:`DavPreconditionFailed` a WebDAV one would.
    """
    if isinstance(body, str):
        body = body.encode("utf-8", errors="replace")

    request = Request(method, url)
    response = Response(status, content=body, request=request)
    return enrich_dav_error(
        HTTPStatusError(
            f"{status} error for {method} {url}", request=request, response=response
        )
    )


def enrich_dav_error(exc: HTTPStatusError) -> HTTPStatusError:
    """Return *exc* re-expressed with the server's explanation attached.

    Returns the original exception untouched when there is nothing to add, so
    non-DAV callers (OCS, the app APIs) are unaffected on every status *except*
    412/423/507. Those three are typed off the status code alone, whatever the
    body: the type says "the server answered 412", not "a Sabre document was
    found". A 412 with no parseable body still becomes a
    :class:`DavPreconditionFailed`, just without the ``Server said:`` suffix.
    That is deliberate -- callers branch on those statuses, and a type that
    appeared only when the body happened to parse would be useless to branch on.
    Nothing downstream is affected either way, since every catch site tests
    ``isinstance`` or ``.response.status_code`` rather than an exact type.
    """
    dav_exception, dav_message = parse_dav_error_body(exc.response)
    status = exc.response.status_code
    error_type = _STATUS_TYPES.get(status)

    if dav_exception is None and dav_message is None and error_type is None:
        return exc

    detail = ": ".join(part for part in (dav_exception, dav_message) if part)
    # Single line on purpose: callers log this with "%s" (webdav.py does), and a
    # newline here would split one failure across two log records -- which line-
    # oriented log shipping then indexes as two unrelated events.
    message = f"{exc} -- Server said: {detail}" if detail else str(exc)

    return (error_type or DavError)(
        message,
        request=exc.request,
        response=exc.response,
        dav_exception=dav_exception,
        dav_message=dav_message,
    )
