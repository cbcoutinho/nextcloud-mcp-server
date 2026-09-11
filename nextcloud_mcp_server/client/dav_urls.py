"""Percent-encode DAV paths and URLs by one rule, for every DAV client.

Paths flow through these clients already URL-decoded -- ``unquote`` on the
``<d:href>`` of a PROPFIND/REPORT response, the principal id discovered by
``BaseNextcloudClient._ensure_principal_id``, or raw user-supplied paths from
MCP tools -- so characters like ``#``, ``,`` and spaces reach the transport
verbatim unless something encodes them.

Two transports, two different ways that hurts:

* **httpx** (Notes, WebDAV, Contacts) parses an unencoded ``#`` as a URL
  fragment and silently truncates the request path -> spurious 404 on an
  otherwise-valid file (PR #891, e.g. filenames with ``#``, commas, or
  double/trailing spaces).
* **caldav** (Calendar) never gets that far: ``DAVObject.__init__`` rejects any
  URL containing a space outright, so every calendar and task operation fails
  before a request is sent (issue #1449).

The convention is therefore: **store decoded, encode exactly once at the point
of use.** Because the input is decoded, a literal ``%`` encodes to ``%25``
rather than being mistaken for an existing escape, and the round trip is
lossless -- including for a path that literally contains the text ``%20``.

These live here rather than beside one client so the two cannot drift: they did
once already, when the calendar copy reached for ``urlsplit`` and reintroduced
the very ``#`` truncation the WebDAV one exists to prevent.
"""

from urllib.parse import quote

__all__ = ["encode_dav_path", "encode_dav_url"]


def encode_dav_path(path: str) -> str:
    """Percent-encode a *decoded* DAV path for use in a request URL or header.

    ``quote`` with ``safe="/"`` encodes the unsafe characters while preserving
    the path separators; an ASCII-only path is unchanged. Everything else --
    ``#`` and ``?`` included -- is a literal character in a DAV path, not a
    delimiter, so it is encoded rather than treated as a fragment/query marker.
    """
    return quote(path, safe="/")


def encode_dav_url(url: str) -> str:
    """Percent-encode the path of a *decoded* DAV path or absolute URL.

    Same rule as :func:`encode_dav_path`; only the authority is split off
    first, so an absolute URL's scheme, host and port survive.
    ``CalendarClient`` stores absolute URLs where ``WebDAVClient`` stores paths.

    The split is deliberately ``partition`` and not ``urlsplit``: everything
    after the authority is a DAV *path*. ``urlsplit`` would read a ``#`` or
    ``?`` in it as a fragment/query delimiter and drop the remainder, silently
    truncating the URL.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return encode_dav_path(url)
    netloc, slash, path = rest.partition("/")
    return f"{scheme}://{netloc}{slash}{encode_dav_path(path)}"
