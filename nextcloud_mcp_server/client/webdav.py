"""WebDAV client for Nextcloud file operations."""

import logging
import mimetypes
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape as xml_escape

import anyio
from httpx import HTTPStatusError, RemoteProtocolError, Response

from nextcloud_mcp_server.observability.metrics import (
    document_download_truncated_total,
    document_scan_truncated_total,
)

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)

# The ``{"OCS-APIRequest": "true"}`` headers throughout this module are
# deliberately NOT ``ocs.OCS_REQUEST_HEADERS``. These are DAV requests
# (PROPFIND / PUT / MKCOL / REPORT / MOVE / COPY), not OCS ones: they send the
# header only to identify themselves as API traffic to Nextcloud's CSRF check,
# and Sabre answers them in XML. The OCS constant also carries
# ``Accept: application/json``, which would be a lie here. Most of these sites
# additionally carry request-specific ``Depth`` / ``Content-Type`` values, so
# there is nothing shared left to hoist -- keep writing them inline. The guard
# in ``tests/unit/test_ocs_headers_are_shared`` only rejects the OCS+JSON
# pairing, so these stay legal by construction rather than by exemption.


class OversizeDownload(Exception):
    """A streamed download exceeded its byte budget and was aborted.

    Distinct from the pre-flight size gate: that acts on the size the server
    advertised at scan time, this acts on what actually arrives, so it still
    holds when ``Content-Length`` is absent or untrue.
    """


def _read_complete_body(response: Response, label: str) -> bytes:
    """Return the response body, raising on a short read vs ``Content-Length``.

    A truncated/desynced response on a pooled keep-alive connection can hand
    back an empty/short body that the document parser then reads as ``0 chars``
    and the vector-sync processor permanently dead-letters (#965). Compare the
    received byte count against the declared ``Content-Length`` and raise a
    retryable :class:`httpx.RemoteProtocolError` on a mismatch: the processor
    retries/re-queues a raised transport error instead of dead-lettering the
    document, so a healthy file recovers on the next scan. (httpx already
    raises on most genuine truncations during the read; this is the
    belt-and-suspenders guard for the cases it returns intact. The robust
    mitigation for connection poisoning is ``NEXTCLOUD_HTTP_KEEPALIVE=false``.)
    A missing or malformed header (e.g. ``Transfer-Encoding: chunked``) skips
    the check so legitimately header-less responses never raise.

    ``Content-Length`` on a compressed response describes the compressed
    size on the wire, not the decompressed ``response.content`` httpx hands
    back — comparing the two would misfire on every compressible file
    Nextcloud happens to gzip, so the check only applies to identity-encoded
    responses (see #1099).
    """
    content = response.content
    _verify_content_length(response, len(content), label)
    return content


def _expected_content_length(response: Response) -> int | None:
    """The comparable ``Content-Length``, or ``None`` when the check can't apply.

    Returns ``None`` for a compressed response (the header describes the
    compressed size on the wire, not the decompressed bytes httpx hands back --
    see #1099), a missing header (e.g. ``Transfer-Encoding: chunked``), a
    malformed one, or a degenerate negative length.
    """
    content_encoding = response.headers.get("content-encoding")
    if content_encoding is not None and content_encoding.lower() != "identity":
        return None
    declared = response.headers.get("content-length")
    if declared is None:
        return None
    try:
        expected = int(declared)
    except ValueError:
        # Malformed header — nothing reliable to compare against, so don't
        # raise spuriously; let the (possibly fine) body through.
        return None
    if expected < 0:
        # Degenerate header (negative length) — can't be a real short-read
        # signal and would always trip the check below; ignore it.
        return None
    return expected


def _verify_content_length(response: Response, received: int, label: str) -> None:
    """Raise :class:`RemoteProtocolError` when ``received`` is a short read.

    Shared by the buffered (:func:`_read_complete_body`) and streaming
    (``WebDavClient.stream_to_file``) download paths so the #965 truncation
    guard and the #1099 content-encoding carve-out cannot drift apart between
    them.
    """
    expected = _expected_content_length(response)
    if expected is None or received == expected:
        return
    document_download_truncated_total.inc()
    # Log here, not just in the message: callers funnel this through a
    # generic ``except Exception`` that would otherwise report it as an
    # opaque "Unexpected error reading file".
    logger.warning(
        "Truncated download for %r: expected %d bytes, got %d "
        "(poisoned keep-alive connection? set NEXTCLOUD_HTTP_KEEPALIVE=false "
        "— see #965)",
        label,
        expected,
        received,
    )
    raise RemoteProtocolError(
        f"Truncated download for {label!r}: expected {expected} bytes, "
        f"got {received} (poisoned keep-alive connection? see #965)",
        request=response.request,
    )


# Paging defaults for WebDAV SEARCH. Nextcloud's SEARCH returns a server-default
# page (~100 results) when no ``<d:nresults>`` is sent, silently truncating large
# folders. ``search_files_all`` pages explicitly to fetch the complete result set.
WEBDAV_SEARCH_PAGE_SIZE = 500
# Hard ceiling so a pathologically large folder can't drive an unbounded crawl.
# Crossing it is logged as a truncation warning (and surfaced via a metric) so the
# cap can never again silently hide files.
WEBDAV_SEARCH_MAX_RESULTS = 50000


def _encode_dav_path(path: str) -> str:
    """Percent-encode a *decoded* DAV path for use in a request URL/header.

    Paths flow through this client already URL-decoded (e.g. ``unquote`` on the
    ``<d:href>`` of a PROPFIND/REPORT response, or raw user-supplied paths from
    MCP tools), so characters like ``#``, ``,`` and spaces reach httpx verbatim.
    An unencoded ``#`` is parsed as a URL fragment and silently truncates the
    request path → spurious 404 on otherwise-valid files (issue: OHR-Bench
    ingest, card 309). ``quote`` with ``safe="/"`` encodes the unsafe characters
    while preserving the path separators; ASCII-only paths are unchanged.

    Encode exactly once: the input is decoded, so a literal ``%`` becomes
    ``%25`` (correct) rather than being mistaken for an existing escape.
    """
    return quote(path, safe="/")


def _reject_path_traversal(path: str) -> str:
    """Reject a DAV path that walks out of the user's home, returning it as-is.

    Every WebDAV request path is built by ``WebDAVClient._webdav_path``, which
    concatenates caller input onto ``/remote.php/dav/files/<principal>``. MCP
    tool arguments reach that unmodified, so a path containing ``..`` segments
    is normalised away by the URL layer *before* the request leaves us and can
    address another user's home or the DAV root
    (pythonsecurity:S2083). There is no legitimate use of ``..`` in a path
    relative to one's own home, so this fails closed rather than normalising:
    silently rewriting the path would turn a caller's mistake into a
    successful operation on a file they did not name.

    Backslashes are folded to ``/`` first — Nextcloud treats ``\\`` as a literal
    filename character, but the URL layer does not, so ``..\\..`` must not slip
    past a ``/``-only split.
    """
    if any(segment == ".." for segment in path.replace("\\", "/").split("/")):
        raise ValueError(
            f"Path may not contain '..' segments; it must stay within the "
            f"user's files: {path!r}"
        )
    return path


def _normalize_etag(raw: Optional[str]) -> Optional[str]:
    """Strip the quotes an ETag is transported in, so callers can pass it back.

    Every etag this client surfaces goes through here — ``read_file``,
    ``list_directory``, the search parser and now ``write_file`` — so the
    representation cannot drift between the value we hand out and the value
    ``_write_precondition_header`` expects to re-quote.

    Two shapes need repairing beyond the quotes:

    *Weak validators.* ``W/"abc"`` used to come out as ``W/"abc`` because
    ``strip`` only removes characters from the *ends* and the leading ``W``
    protected the opening quote — usable as neither an etag nor an ``If-Match``
    value (RFC 9110 requires strong comparison there anyway). The prefix is now
    removed first.

    *Content-coding suffixes.* Apache's ``mod_deflate`` appends ``-gzip`` to the
    ETag of every compressed response unless ``DeflateAlterETag NoChange`` is
    set; ``AddSuffix`` is the default and the directive did not exist before
    Apache 2.4.15, so many deployments never set it. The etag a caller reads
    back is then not the etag the origin stored, and handing it to ``If-Match``
    makes Nextcloud reject the write — so every overwrite of an existing file
    fails behind such a proxy, with an error that reads like a real conflict.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("W/"):
        cleaned = cleaned[2:]
    cleaned = cleaned.strip('"')
    for suffix in ("-gzip", "-br", "-deflate"):
        if cleaned.endswith(suffix):
            return cleaned[: -len(suffix)]
    return cleaned


#: Collection holding one file's comments, addressed by file id.
_COMMENTS_PATH = "/remote.php/dav/comments/files"


def _dav_props_ok(response_elem: ET.Element) -> dict[str, str]:
    """Return a multistatus response's ``200 OK`` properties, keyed by local name.

    A response carries one ``propstat`` block per status: the 200 one holds the
    values, while a 404 one lists the properties the server does *not* have.
    Folding those in would fabricate empty values for them.
    """
    props: dict[str, str] = {}
    for propstat in response_elem.findall("{DAV:}propstat"):
        # "HTTP/1.1 200 OK" -> the code is the second token. Matching the token
        # rather than a " 200 " substring keeps a reason-phrase-less status line
        # (legal per RFC 9110) working.
        status = (propstat.findtext("{DAV:}status") or "").split()
        if len(status) < 2 or status[1] != "200":
            continue
        prop = propstat.find("{DAV:}prop")
        if prop is None:
            continue
        for child in prop:
            props[str(child.tag).rsplit("}", 1)[-1]] = (child.text or "").strip()
    return props


def _parse_comment_props(props: dict[str, str]) -> dict[str, Any] | None:
    """Shape one comment's DAV properties into the dict ``FileComment`` consumes.

    Returns None for a row without a usable id — one malformed comment should
    cost the caller that comment, not the whole thread.
    """
    try:
        comment_id = int(props["id"])
    except (KeyError, ValueError):
        logger.warning("Skipping comment with unusable id %r", props.get("id"))
        return None
    return {
        "id": comment_id,
        "message": props.get("message", ""),
        "actor_id": props.get("actorId", ""),
        "actor_type": props.get("actorType", ""),
        "actor_display_name": props.get("actorDisplayName") or None,
        # Raw DAV date (RFC 1123), like every other timestamp this client
        # surfaces -- see FileInfo.last_modified.
        "creation_datetime": props.get("creationDateTime") or None,
        "verb": props.get("verb", ""),
        "is_unread": props.get("isUnread", "").lower() == "true",
    }


def _destination_precondition_header(
    destination_webdav_path: str, if_destination_match: str
) -> dict[str, str]:
    """Build the tagged-list ``If:`` header that conditions the *destination*.

    ``If-Match`` is the obvious choice here and it is **wrong**: per RFC 9110 it
    applies to the request-URI, which for MOVE/COPY is the *source*. Conditioning
    the destination needs RFC 4918 §10.4's tagged-list form, where the resource
    the condition applies to is named explicitly::

        If: <destination-uri> (["etag"])

    Three details are load-bearing, all dictated by sabre/dav's parser
    (``Server::getIfConditions``)::

        /(?:\\<(?P<uri>.*?)\\>\\s)?\\((?P<not>Not\\s)?...(?:\\[(?P<etag>[^\\]]*)\\])?\\)/im

    * the **space after ``>``** is required by the regex — without it the URI is
      not captured and the condition silently applies to the request-URI instead,
      i.e. it guards the wrong resource;
    * the etag **keeps its quotes inside the brackets**, because the comparison is
      ``$node->getETag() == $token['etag']`` and Nextcloud's ``Node::getETag()``
      returns a quoted value;
    * the URI is resolved with ``calculateUri()``, so it must be the same
      percent-encoded absolute DAV path used in ``Destination``.
    """
    return {"If": f'<{destination_webdav_path}> (["{if_destination_match}"])'}


def _validate_destination_precondition(
    if_destination_match: Optional[str], overwrite: bool
) -> None:
    """Reject combinations the WebDAV ``If:`` grammar cannot express.

    Raised at the client boundary rather than silently reinterpreted, so a
    caller never believes a guard is in force when it is not.
    """
    if if_destination_match is None:
        return
    if if_destination_match == "*":
        raise ValueError(
            "if_destination_match='*' is not expressible: RFC 4918's tagged-list "
            "If: grammar has no wildcard, and 'the destination must exist' cannot "
            "be asserted this way — an If: condition naming a missing URI returns "
            "404, not 412. Use overwrite=True without a destination etag."
        )
    if not overwrite:
        raise ValueError(
            "if_destination_match with overwrite=False is contradictory: the etag "
            "asserts which version of the destination to replace, while "
            "overwrite=False refuses to replace it at all. Pass overwrite=True to "
            "replace exactly that version."
        )


def like_predicate(prop_element: str, value: str) -> str:
    """Build a ``<d:like>`` SEARCH predicate with *value* XML-escaped.

    Every caller-supplied literal in a SEARCH body goes through here. A value
    containing ``&``, ``<`` or ``>`` -- "Costs & Revenue%" is the everyday case
    -- otherwise produces malformed XML that Sabre/DAV rejects with 400, and
    the search fails rather than returning nothing.

    It exists as a shared builder rather than an escaping call each site
    remembers to make: the same predicate was being assembled in five places,
    two of which had no escaping at all. Routing construction through one
    function makes the escaping structural instead of a convention.

    Args:
        prop_element: Namespaced property element, e.g. ``"d:displayname"`` or
            ``"oc:tags"``. Not caller-supplied -- callers pass a literal.
        value: The match value, including any ``%`` wildcards. Escaped here.
    """
    return (
        "<d:like>"
        f"<d:prop><{prop_element}/></d:prop>"
        f"<d:literal>{xml_escape(value)}</d:literal>"
        "</d:like>"
    )


def _write_precondition_header(if_match: Optional[str]) -> dict[str, str]:
    """Pick the single conditional header for a fail-closed PUT.

    A write always carries a precondition so it can never silently clobber an
    existing file (the server evaluates it atomically before the PUT):

    - ``None``  -> ``If-None-Match: *`` (create-only)
    - ``"*"``   -> ``If-Match: *`` (force-overwrite an existing file)
    - an etag   -> ``If-Match: "<etag>"`` (overwrite iff unchanged)
    """
    if if_match is None:
        return {"If-None-Match": "*"}
    if if_match == "*":
        return {"If-Match": "*"}
    return {"If-Match": f'"{_normalize_etag(if_match)}"'}


def _write_conflict_result(
    if_match: Optional[str], status_code: int, path: str
) -> Optional[Dict[str, Any]]:
    """Map a known write-conflict status to a structured result, else ``None``.

    412 and 423 are conditions a caller must react to (not transport failures),
    so :meth:`WebDAVClient.write_file` returns them rather than raising, matching
    ``move_resource``/``copy_resource``. Which 412 message applies depends on
    which precondition we sent -- the client knows, so it gives a cause-specific,
    actionable message instead of parsing the server's response body. Any other
    status returns ``None`` so the caller re-raises.
    """
    if status_code == 412:
        if if_match is None:
            logger.debug(
                "Precondition failed writing '%s': file already exists "
                "(create-only If-None-Match)",
                path,
            )
            return {
                "status_code": 412,
                "message": "File already exists — read it first to get its etag "
                "and pass if_match to overwrite safely, or pass if_match='*' to "
                "overwrite deliberately",
            }
        if if_match == "*":
            logger.debug(
                "Precondition failed writing '%s': file does not exist "
                "(force-overwrite If-Match: *)",
                path,
            )
            return {
                "status_code": 412,
                "message": "File does not exist — cannot force-overwrite a missing "
                "file; omit if_match to create it",
            }
        logger.debug(
            "Precondition failed writing '%s': file changed since if_match etag "
            "was read",
            path,
        )
        return {
            "status_code": 412,
            "message": "File was modified since the given etag was read "
            "(concurrent edit) — re-read before writing",
        }
    if status_code == 423:
        logger.debug("Resource locked writing '%s'", path)
        return {
            "status_code": 423,
            "message": "File is locked by another client (e.g. open in the "
            "Nextcloud web editor) — not retried automatically",
        }
    return None


class WebDAVClient(BaseNextcloudClient):
    """Client for Nextcloud WebDAV operations."""

    app_name = "webdav"

    def _webdav_path(self, path: str) -> str:
        """Build the request path for ``path`` under the user's DAV root.

        Percent-encodes the caller-supplied portion (see ``_encode_dav_path``)
        so names with ``#``, commas, or spaces don't truncate/404; the base
        ``/remote.php/dav/files/<principal>`` segment is left as-is.

        Precondition: ``path`` is a **decoded** path (the convention everywhere
        in this client — PROPFIND/REPORT hrefs are ``unquote``d before storage,
        and MCP-tool inputs are raw). It is encoded exactly once, so passing an
        already-encoded path would double-encode it (``%20`` → ``%2520``).

        Raises ``ValueError`` for a path containing ``..``. This is the single
        chokepoint every WebDAV request path goes through, so guarding here
        covers read/write/delete/move/copy/list at once rather than per caller.
        """
        safe_path = _reject_path_traversal(path)
        return (
            f"{self._get_webdav_base_path()}/{_encode_dav_path(safe_path.lstrip('/'))}"
        )

    async def delete_resource(self, path: str) -> Dict[str, Any]:
        """Delete a resource (file or directory) via WebDAV DELETE."""
        await self._ensure_principal_id()
        # Ensure path ends with a slash if it's a directory
        if not path.endswith("/"):
            path_with_slash = f"{path}/"
        else:
            path_with_slash = path

        webdav_path = self._webdav_path(path_with_slash)
        logger.debug("Deleting WebDAV resource: %s", webdav_path)

        headers = {"OCS-APIRequest": "true"}
        try:
            # First try a PROPFIND to verify resource exists
            propfind_headers = {"Depth": "0", "OCS-APIRequest": "true"}
            try:
                propfind_resp = await self._make_request(
                    "PROPFIND", webdav_path, headers=propfind_headers
                )
                logger.debug(
                    "Resource exists check status: %s", propfind_resp.status_code
                )
            except HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.debug(
                        "Resource '%s' doesn't exist, no deletion needed", path
                    )
                    return {"status_code": 404}
                # For other errors, continue with deletion attempt

            # Proceed with deletion
            response = await self._make_request("DELETE", webdav_path, headers=headers)
            logger.debug("Successfully deleted WebDAV resource '%s'", path)
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug("Resource '%s' not found, no deletion needed", path)
                return {"status_code": 404}
            else:
                logger.error("HTTP error deleting WebDAV resource '%s': %s", path, e)
                raise e
        except Exception as e:
            logger.error("Unexpected error deleting WebDAV resource '%s': %s", path, e)
            raise e

    async def cleanup_old_attachment_directory(
        self, note_id: int, old_category: str
    ) -> Dict[str, Any]:
        """Remove the husk left at a note's old category after a category change.

        Only removes the directory if it is EMPTY. The Notes app relocates
        ``.attachments.<note_id>`` to the new category itself as part of the
        category change, so by the time this runs the old path is normally
        already gone. Anything still *in* it is live attachment data the server
        has not moved yet — deleting that would destroy the user's attachments
        rather than tidy up after them, and the directory the caller wants gone
        would take the files with it.
        """
        old_category_path_part = f"{old_category}/" if old_category else ""
        old_attachment_dir_path = (
            f"Notes/{old_category_path_part}.attachments.{note_id}/"
        )

        logger.debug(
            "Cleaning up old attachment directory: %s", old_attachment_dir_path
        )
        try:
            try:
                remaining = await self.list_directory(old_attachment_dir_path)
            except HTTPStatusError as e:
                if e.response.status_code != 404:
                    raise
                # Already gone (the normal case — the server moved it). Fall
                # through to the DELETE, which no-ops on 404.
                remaining = []

            if remaining:
                logger.warning(
                    "Refusing to delete old attachment directory %s for note %s: "
                    "it still holds %d item(s), so the server has not relocated "
                    "them to the new category yet. Leaving it in place — deleting "
                    "it here would destroy those attachments.",
                    old_attachment_dir_path,
                    note_id,
                    len(remaining),
                )
                # 412: we refused because the "directory is empty" precondition
                # did not hold. Keeps the {"status_code": int} shape callers see.
                return {"status_code": 412, "deleted": False}

            # ponytail: probe-then-delete, not an atomic conditional delete —
            # a write into this old path between the two calls would still be
            # removed. WebDAV has no "delete only if empty" (DELETE on a
            # collection is always infinite-depth), so closing it means an
            # If-Match on the collection etag. Not worth it for a window that
            # needs someone writing into the *previous* category of a note that
            # just moved; upgrade if that ever shows up in practice.
            delete_result = await self.delete_resource(path=old_attachment_dir_path)
            logger.debug("Cleanup result: %s", delete_result)
            return delete_result
        except Exception as e:
            logger.error("Error during cleanup of old attachment directory: %s", e)
            raise e

    async def cleanup_note_attachments(
        self, note_id: int, category: str
    ) -> Dict[str, Any]:
        """Clean up attachment directory for a specific note and category."""
        cat_path_part = f"{category}/" if category else ""
        attachment_dir_path = f"Notes/{cat_path_part}.attachments.{note_id}/"

        logger.debug(
            "Cleaning up attachments for note %s in category '%s'", note_id, category
        )
        try:
            delete_result = await self.delete_resource(path=attachment_dir_path)
            logger.debug("Cleanup result for note %s: %s", note_id, delete_result)
            return delete_result
        except Exception as e:
            logger.error("Failed cleaning up attachments for note %s: %s", note_id, e)
            raise e

    async def add_note_attachment(
        self,
        note_id: int,
        filename: str,
        content: bytes,
        category: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add/Update an attachment to a note via WebDAV PUT."""
        await self._ensure_principal_id()
        # Construct paths based on provided category. Encode via _webdav_path so
        # categories/filenames with '#', commas or spaces don't truncate/404.
        category_path_part = f"{category}/" if category else ""
        attachment_dir_segment = f".attachments.{note_id}"
        parent_dir_webdav_rel_path = (
            f"Notes/{category_path_part}{attachment_dir_segment}"
        )
        parent_dir_path = self._webdav_path(parent_dir_webdav_rel_path)
        attachment_path = self._webdav_path(f"{parent_dir_webdav_rel_path}/{filename}")

        logger.debug("Uploading attachment '%s' for note %s", filename, note_id)

        if not mime_type:
            mime_type, _ = mimetypes.guess_type(filename)
            if not mime_type:
                mime_type = "application/octet-stream"

        headers = {"Content-Type": mime_type, "OCS-APIRequest": "true"}
        try:
            # First check if we can access WebDAV at all
            notes_dir_path = self._webdav_path("Notes")
            propfind_headers = {"Depth": "0", "OCS-APIRequest": "true"}
            notes_dir_response = await self._make_request(
                "PROPFIND", notes_dir_path, headers=propfind_headers
            )

            if notes_dir_response.status_code == 401:
                logger.error("WebDAV authentication failed for Notes directory")
                raise HTTPStatusError(
                    f"Authentication error accessing WebDAV Notes directory: {notes_dir_response.status_code}",
                    request=notes_dir_response.request,
                    response=notes_dir_response,
                )
            elif notes_dir_response.status_code >= 400:
                logger.error(
                    "Error accessing WebDAV Notes directory: %s",
                    notes_dir_response.status_code,
                )
                notes_dir_response.raise_for_status()

            # Ensure the parent directory exists using MKCOL
            mkcol_headers = {"OCS-APIRequest": "true"}
            mkcol_response = await self._make_request(
                "MKCOL", parent_dir_path, headers=mkcol_headers
            )

            # MKCOL should return 201 Created or 405 Method Not Allowed (if directory already exists)
            if mkcol_response.status_code not in [201, 405]:
                logger.error(
                    "Unexpected status code %s when creating attachments directory",
                    mkcol_response.status_code,
                )
                mkcol_response.raise_for_status()

            # Proceed with the PUT request
            response = await self._make_request(
                "PUT", attachment_path, content=content, headers=headers
            )
            response.raise_for_status()
            logger.debug(
                "Successfully uploaded attachment '%s' to note %s", filename, note_id
            )
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            logger.error(
                "HTTP error uploading attachment '%s' to note %s: %s",
                filename,
                note_id,
                e,
            )
            raise e
        except Exception as e:
            logger.error(
                "Unexpected error uploading attachment '%s' to note %s: %s",
                filename,
                note_id,
                e,
            )
            raise e

    async def get_note_attachment(
        self, note_id: int, filename: str, category: Optional[str] = None
    ) -> Tuple[bytes, str]:
        """Fetch a specific attachment from a note via WebDAV GET."""
        await self._ensure_principal_id()
        category_path_part = f"{category}/" if category else ""
        attachment_dir_segment = f".attachments.{note_id}"
        attachment_path = self._webdav_path(
            f"Notes/{category_path_part}{attachment_dir_segment}/{filename}"
        )

        logger.debug("Fetching attachment '%s' for note %s", filename, note_id)

        try:
            response = await self._make_request("GET", attachment_path)
            response.raise_for_status()

            content = _read_complete_body(response, filename)
            mime_type = response.headers.get("content-type", "application/octet-stream")

            logger.debug(
                "Successfully fetched attachment '%s' (%s bytes)",
                filename,
                len(content),
            )
            return content, mime_type

        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug("Attachment '%s' not found for note %s", filename, note_id)
            else:
                logger.error(
                    "HTTP error fetching attachment '%s' for note %s: %s",
                    filename,
                    note_id,
                    e,
                )
            raise e
        except Exception as e:
            logger.error(
                "Unexpected error fetching attachment '%s' for note %s: %s",
                filename,
                note_id,
                e,
            )
            raise e

    async def list_directory(self, path: str = "") -> List[Dict[str, Any]]:
        """List files and directories in the specified path via WebDAV PROPFIND."""
        await self._ensure_principal_id()
        webdav_path = self._webdav_path(path)
        if not webdav_path.endswith("/"):
            webdav_path += "/"

        logger.debug("Listing directory: %s", path)

        # oc:fileid is requested alongside the DAV properties because it is the
        # only stable identity a browser link can use (/index.php/f/<id>); paths
        # move and rename. The search PROPFINDs below already ask for it.
        propfind_body = """<?xml version="1.0"?>
        <d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
            <d:prop>
                <d:displayname/>
                <d:getcontentlength/>
                <d:getcontenttype/>
                <d:getlastmodified/>
                <d:getetag/>
                <d:resourcetype/>
                <oc:fileid/>
            </d:prop>
        </d:propfind>"""

        headers = {"Depth": "1", "Content-Type": "text/xml", "OCS-APIRequest": "true"}

        try:
            response = await self._make_request(
                "PROPFIND", webdav_path, content=propfind_body, headers=headers
            )
            response.raise_for_status()

            # Parse the XML response
            root = ET.fromstring(response.content)
            items = []

            # Skip the first response (the directory itself)
            responses = root.findall(".//{DAV:}response")[1:]

            for response_elem in responses:
                href = response_elem.find(".//{DAV:}href")
                if href is None:
                    continue

                # Extract file/directory name from href. <d:href> is required by
                # RFC 3986 to be percent-encoded, so non-ASCII names arrive
                # encoded — decode before exposing to callers (issue #776).
                href_text = href.text or ""
                name = unquote(href_text.rstrip("/").split("/")[-1])
                if not name:
                    continue

                # Get properties
                propstat = response_elem.find(".//{DAV:}propstat")
                if propstat is None:
                    continue

                prop = propstat.find(".//{DAV:}prop")
                if prop is None:
                    continue

                # Determine if it's a directory
                resourcetype = prop.find(".//{DAV:}resourcetype")
                is_directory = (
                    resourcetype is not None
                    and resourcetype.find(".//{DAV:}collection") is not None
                )

                # Get other properties
                size_elem = prop.find(".//{DAV:}getcontentlength")
                size = (
                    int(size_elem.text)
                    if size_elem is not None and size_elem.text
                    else 0
                )

                content_type_elem = prop.find(".//{DAV:}getcontenttype")
                content_type = (
                    content_type_elem.text if content_type_elem is not None else None
                )

                modified_elem = prop.find(".//{DAV:}getlastmodified")
                modified = modified_elem.text if modified_elem is not None else None

                # Strip surrounding quotes to match every other etag path in
                # this client (a caller feeds this straight into write_file's
                # if_match, which re-adds the quotes for the If-Match header).
                etag_elem = prop.find(".//{DAV:}getetag")
                etag = (
                    _normalize_etag(etag_elem.text)
                    if etag_elem is not None and etag_elem.text
                    else None
                )

                # Parsed defensively, unlike the sizes above: file_id only feeds
                # the deep link, so a server that ever returns a non-numeric one
                # should cost the caller that link, not the whole listing.
                fileid_elem = prop.find(".//{http://owncloud.org/ns}fileid")
                file_id = None
                if fileid_elem is not None and fileid_elem.text:
                    try:
                        file_id = int(fileid_elem.text)
                    except ValueError:
                        logger.warning(
                            "Ignoring non-numeric oc:fileid %r for %s",
                            fileid_elem.text,
                            name,
                        )

                items.append(
                    {
                        "name": name,
                        "path": f"{path.rstrip('/')}/{name}" if path else name,
                        "is_directory": is_directory,
                        "size": size if not is_directory else None,
                        "content_type": content_type,
                        "last_modified": modified,
                        "etag": etag,
                        "file_id": file_id,
                    }
                )

            logger.debug("Found %s items in directory: %s", len(items), path)
            return items

        except HTTPStatusError as e:
            # A missing directory is an expected outcome for callers that probe
            # before acting (see cleanup_old_attachment_directory), not a fault.
            if e.response.status_code == 404:
                logger.debug("Directory '%s' not found", webdav_path)
            else:
                logger.error("HTTP error listing directory '%s': %s", webdav_path, e)
            raise e
        except Exception as e:
            logger.error("Unexpected error listing directory '%s': %s", webdav_path, e)
            raise e

    async def stream_to_file(
        self, path: str, dest: Path, *, max_bytes: int | None = None
    ) -> Tuple[int, str, Optional[str]]:
        """Stream a WebDAV GET straight to ``dest``, never holding the whole body.

        :meth:`read_file` buffers the entire response (``response.content``), so
        peak memory scales with file size -- a 531 MB PDF OOMKilled an ingest
        worker mid-download. Streaming keeps resident memory at one chunk
        regardless of how large the document is.

        ``max_bytes`` aborts the transfer as soon as the limit is exceeded and
        removes the partial file. This is the guard that survives an absent or
        untrue ``Content-Length``: the pre-flight size gate can only act on what
        the server advertised at scan time, whereas this acts on what actually
        arrives.

        Returns ``(bytes_written, content_type, etag)`` -- the same triple
        :meth:`read_file` returns, so a caller that streams a document can still
        hand the etag back as ``write_file``'s ``if_match`` (or report it) without
        a second request. The etag is ``None`` if the server sent no ``ETag``.
        Raises :class:`OversizeDownload` if ``max_bytes`` is exceeded, or
        :class:`httpx.RemoteProtocolError` on a short read (#965).
        """
        await self._ensure_principal_id()
        webdav_path = self._webdav_path(path)

        logger.debug("Streaming file to %s: %s", dest, path)

        written = 0
        try:
            async with self._stream_request("GET", webdav_path) as response:
                content_type = response.headers.get(
                    "content-type", "application/octet-stream"
                )
                etag = _normalize_etag(response.headers.get("etag"))
                # anyio's async file wrapper, not pathlib.Path.open: the writes
                # run on a worker thread, so streaming a multi-hundred-MB
                # document does not block the event loop (and with it every other
                # in-flight job on this worker) for the duration of the download.
                async with await anyio.open_file(dest, "wb") as fh:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if max_bytes is not None and written > max_bytes:
                            raise OversizeDownload(
                                f"Download of {path!r} exceeded {max_bytes} bytes "
                                f"(aborted after {written})"
                            )
                        await fh.write(chunk)
                # Same short-read guard as the buffered path, against bytes
                # written rather than bytes held in memory.
                _verify_content_length(response, written, path)
        except BaseException:
            # Never leave a partial or over-cap file behind for the parser to
            # read as a valid (truncated) document.
            dest.unlink(missing_ok=True)
            raise

        logger.debug("Streamed '%s' to %s (%s bytes)", path, dest, written)
        return written, content_type, etag

    async def read_file(self, path: str) -> Tuple[bytes, str, Optional[str]]:
        """Read a file's content via WebDAV GET.

        Returns the ``ETag`` alongside the content so a caller that intends to
        write the file back can pass it as ``write_file``'s ``if_match`` and
        detect a concurrent edit (made e.g. directly in the Nextcloud web UI)
        instead of silently overwriting it -- there was previously no way for
        a caller to even notice that race. The etag is ``None`` if the server
        did not return an ``ETag`` header.
        """
        await self._ensure_principal_id()
        webdav_path = self._webdav_path(path)

        logger.debug("Reading file: %s", path)

        try:
            response = await self._make_request("GET", webdav_path)
            response.raise_for_status()

            content = _read_complete_body(response, path)
            content_type = response.headers.get(
                "content-type", "application/octet-stream"
            )
            etag = _normalize_etag(response.headers.get("etag"))

            logger.debug("Successfully read file '%s' (%s bytes)", path, len(content))
            return content, content_type, etag

        except HTTPStatusError as e:
            logger.error("HTTP error reading file '%s': %s", path, e)
            raise e
        except Exception as e:
            logger.error("Unexpected error reading file '%s': %s", path, e)
            raise e

    async def write_file(
        self,
        path: str,
        content: bytes,
        content_type: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write content to a file via WebDAV PUT.

        Every PUT is conditional: the write is fail-closed so an existing file
        is never silently overwritten. Exactly one precondition header is sent,
        chosen by ``if_match``:

        =================  ====================  ======================================
        ``if_match``       Header sent           Behaviour (412 = precondition failed)
        =================  ====================  ======================================
        ``None`` (default) ``If-None-Match: *``  Create-only. 412 if the path already
                                                 exists -- read it first to get an etag.
        an etag, e.g. abc  ``If-Match: "abc"``   Conditional overwrite. 412 if the file
                                                 changed since that etag (or is gone).
        ``"*"``            ``If-Match: *``       Force-overwrite an existing file. 412 if
                                                 it does not exist.
        =================  ====================  ======================================

        Args:
            path: Destination path.
            content: Raw bytes to write.
            content_type: MIME type (guessed from ``path`` if omitted).
            if_match: ``None`` to create a new file (fails if it exists); an
                etag from :meth:`read_file` to overwrite only if unchanged; or
                the literal ``"*"`` to force-overwrite an existing file.

        Returns:
            ``{"status_code": ...}`` on success. On a 412 (a precondition above
            failed) or a 423 (WebDAV lock held by another client, e.g. the file
            is open in the Nextcloud web editor), returns ``{"status_code": ...,
            "message": ...}`` instead of raising, matching ``move_resource``/
            ``copy_resource``'s handling of their own known conflict statuses
            -- both are conditions a caller should react to, not a transport
            failure. Any other error status still raises ``HTTPStatusError``.
        """
        await self._ensure_principal_id()
        webdav_path = self._webdav_path(path)

        logger.debug("Writing file: %s", path)

        if not content_type:
            content_type, _ = mimetypes.guess_type(path)
            if not content_type:
                content_type = "application/octet-stream"

        headers = {"Content-Type": content_type, "OCS-APIRequest": "true"}
        # Always send a precondition so a write can never silently clobber an
        # existing file. The server (Sabre checkPreconditions) evaluates it
        # atomically before the PUT, so this is race-free.
        headers.update(_write_precondition_header(if_match))

        try:
            response = await self._make_request(
                "PUT", webdav_path, content=content, headers=headers
            )
            response.raise_for_status()

            logger.debug("Successfully wrote file '%s'", path)
            # Surface the new etag so a read-modify-write loop can chain writes
            # without a re-GET. Nextcloud also sends OC-ETag; prefer the standard
            # header and fall back. May be None if a proxy stripped both, in
            # which case the caller must re-read before its next conditional
            # write.
            return {
                "status_code": response.status_code,
                "etag": _normalize_etag(
                    response.headers.get("etag") or response.headers.get("oc-etag")
                ),
            }

        except HTTPStatusError as e:
            # 412/423 are actionable conflicts the caller must handle -> return
            # a structured result. Anything else is a genuine transport error.
            conflict = _write_conflict_result(if_match, e.response.status_code, path)
            if conflict is not None:
                return conflict
            logger.error("HTTP error writing file '%s': %s", path, e)
            raise e
        except Exception as e:
            logger.error("Unexpected error writing file '%s': %s", path, e)
            raise e

    async def create_directory(
        self, path: str, recursive: bool = False
    ) -> Dict[str, Any]:
        """Create a directory via WebDAV MKCOL."""
        await self._ensure_principal_id()
        webdav_path = self._webdav_path(path)
        if not webdav_path.endswith("/"):
            webdav_path += "/"

        logger.debug("Creating directory: %s", path)

        headers = {"OCS-APIRequest": "true"}

        try:
            response = await self._make_request("MKCOL", webdav_path, headers=headers)
            response.raise_for_status()

            logger.debug("Successfully created directory '%s'", path)
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            # Method Not Allowed - directory already exists
            if e.response.status_code == 405:
                logger.debug("Directory '%s' already exists", path)
                return {"status_code": 405, "message": "Directory already exists"}

            # File Conflict - parent directory does not exist
            if e.response.status_code == 409 and recursive:
                # Extract parent directory path
                path_parts = path.strip("/").split("/")
                if len(path_parts) > 1:
                    parent_dir = "/".join(path_parts[:-1])
                    logger.debug(
                        "Parent directory '%s' doesn't exist, creating recursively",
                        parent_dir,
                    )
                    await self.create_directory(parent_dir, recursive)
                    # Now try to create the original directory again
                    return await self.create_directory(path, recursive)
                else:
                    # This shouldn't happen for single-level directories under root
                    logger.error("409 conflict for single-level directory '%s'", path)
                    raise e

            logger.error("HTTP error creating directory '%s': %s", path, e)
            raise e
        except Exception as e:
            logger.error("Unexpected error creating directory '%s': %s", path, e)
            raise e

    @staticmethod
    def _transfer_conflict_result(
        status_code: int,
        if_destination_match: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Map a known MOVE/COPY conflict status to a structured result, else None.

        Mirrors ``_write_conflict_result``. ``None`` means "not a conflict we
        model" — the caller re-raises.

        404 and 412 mean different things depending on whether a destination etag
        was sent, because sabre resolves the tagged ``If:`` URI with
        ``getNodeForPath``: a missing destination raises NotFound and surfaces as
        404, not 412.
        """
        if status_code == 404:
            if if_destination_match is not None:
                return {
                    "status_code": 404,
                    "message": (
                        "Source not found, or the destination whose etag was "
                        "asserted does not exist (an If: condition naming a "
                        "missing URI yields 404, not 412)."
                    ),
                }
            return {"status_code": 404, "message": "Source resource not found"}
        if status_code == 412:
            if if_destination_match is not None:
                return {
                    "status_code": 412,
                    "message": (
                        "Destination changed since that etag was read — re-read "
                        "it before retrying. Note the etag condition applies to "
                        "files only: a directory destination always fails this "
                        "check."
                    ),
                }
            return {
                "status_code": 412,
                "message": "Destination already exists and overwrite is false",
            }
        if status_code == 409:
            return {
                "status_code": 409,
                "message": "Parent directory of destination doesn't exist",
            }
        return None

    async def _transfer_resource(
        self,
        method: str,
        source_path: str,
        destination_path: str,
        overwrite: bool = False,
        *,
        if_destination_match: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Shared implementation of WebDAV MOVE and COPY.

        The two verbs differ only in the HTTP method and log wording — path
        normalisation, header construction, the destination precondition and the
        conflict mapping are identical, so they live here rather than being
        maintained (and drifting) in two places.
        """
        await self._ensure_principal_id()
        source_webdav_path = self._webdav_path(source_path)
        destination_webdav_path = self._webdav_path(destination_path)

        # Ensure paths have consistent trailing slashes for directories
        if source_path.endswith("/") and not destination_path.endswith("/"):
            destination_webdav_path += "/"
        elif not source_path.endswith("/") and destination_path.endswith("/"):
            source_webdav_path += "/"

        logger.debug(
            "%s resource from '%s' to '%s'", method, source_path, destination_path
        )

        _validate_destination_precondition(if_destination_match, overwrite)

        headers = {
            "OCS-APIRequest": "true",
            "Destination": destination_webdav_path,
            "Overwrite": "T" if overwrite else "F",
        }
        if if_destination_match is not None:
            headers.update(
                _destination_precondition_header(
                    destination_webdav_path, if_destination_match
                )
            )

        try:
            response = await self._make_request(
                method, source_webdav_path, headers=headers
            )
            response.raise_for_status()
            logger.debug(
                "%s succeeded from '%s' to '%s'", method, source_path, destination_path
            )
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            conflict = self._transfer_conflict_result(
                e.response.status_code, if_destination_match
            )
            if conflict is not None:
                logger.debug(
                    "%s conflict (%s) from '%s' to '%s'",
                    method,
                    e.response.status_code,
                    source_path,
                    destination_path,
                )
                return conflict
            logger.error(
                "HTTP error on %s from '%s' to '%s': %s",
                method,
                source_path,
                destination_path,
                e,
            )
            raise e
        except Exception as e:
            logger.error(
                "Unexpected error on %s from '%s' to '%s': %s",
                method,
                source_path,
                destination_path,
                e,
            )
            raise e

    async def move_resource(
        self,
        source_path: str,
        destination_path: str,
        overwrite: bool = False,
        *,
        if_destination_match: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move or rename a resource (file or directory) via WebDAV MOVE.

        Args:
            source_path: The path of the file or directory to move
            destination_path: The new path for the file or directory
            overwrite: Whether to overwrite the destination if it exists
            if_destination_match: Optional ETag of the destination. When given,
                the move replaces the destination only if it is still that exact
                version, closing the window where ``overwrite=True`` clobbers a
                file someone else changed in the meantime.

                Requires ``overwrite=True`` (the two are contradictory otherwise)
                and does not accept ``"*"``; both raise ``ValueError``.

                Two limitations, both from sabre/dav and neither hideable: the
                etag condition is evaluated only for files
                (``$node instanceof IFile``), so a **directory** destination
                always fails it with 412; and an ``If:`` condition naming a URI
                that does not exist yields **404, not 412**.

        Returns:
            Dict with status_code and optional message
        """
        return await self._transfer_resource(
            "MOVE",
            source_path,
            destination_path,
            overwrite,
            if_destination_match=if_destination_match,
        )

    async def copy_resource(
        self,
        source_path: str,
        destination_path: str,
        overwrite: bool = False,
        *,
        if_destination_match: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Copy a resource (file or directory) via WebDAV COPY.

        Args:
            source_path: The path of the file or directory to copy
            destination_path: The destination path for the copy
            overwrite: Whether to overwrite the destination if it exists
            if_destination_match: Optional ETag of the destination. When given,
                the copy replaces the destination only if it is still that exact
                version, closing the window where ``overwrite=True`` clobbers a
                file someone else changed in the meantime.

                Requires ``overwrite=True`` (the two are contradictory otherwise)
                and does not accept ``"*"``; both raise ``ValueError``.

                Two limitations, both from sabre/dav and neither hideable: the
                etag condition is evaluated only for files
                (``$node instanceof IFile``), so a **directory** destination
                always fails it with 412; and an ``If:`` condition naming a URI
                that does not exist yields **404, not 412**.

        Returns:
            Dict with status_code and optional message
        """
        return await self._transfer_resource(
            "COPY",
            source_path,
            destination_path,
            overwrite,
            if_destination_match=if_destination_match,
        )

    async def search_files(
        self,
        scope: str = "",
        where_conditions: Optional[str] = None,
        properties: Optional[List[str]] = None,
        order_by: Optional[List[Tuple[str, str]]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Search for files using WebDAV SEARCH method (RFC 5323).

        Args:
            scope: Directory path to search in (empty string for user root)
            where_conditions: XML string for where clause conditions
            properties: List of property names to retrieve (defaults to basic set)
            order_by: List of (property, direction) tuples for sorting, e.g. [("getlastmodified", "descending")]
            limit: Maximum number of results to return
            offset: Number of leading results to skip (``<d:firstresult>``). Note
                that not every Nextcloud release honours offset paging; callers
                that need guaranteed completeness should use ``search_files_all``,
                which detects an ignored offset and falls back.

        Returns:
            List of file/directory dictionaries with requested properties
        """
        await self._ensure_principal_id()
        # Default properties if not specified
        if properties is None:
            properties = [
                "displayname",
                "getcontentlength",
                "getcontenttype",
                "getlastmodified",
                "resourcetype",
                "getetag",
            ]

        # Build the SEARCH request XML
        search_body = self._build_search_xml(
            scope=scope,
            where_conditions=where_conditions,
            properties=properties,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )

        # The SEARCH endpoint is at the dav root
        search_path = "/remote.php/dav/"

        headers = {"Content-Type": "text/xml", "OCS-APIRequest": "true"}

        logger.debug("Searching files in scope: %s", scope)

        try:
            response = await self._make_request(
                "SEARCH", search_path, content=search_body, headers=headers
            )
            response.raise_for_status()

            # Parse the XML response
            results = self._parse_search_response(response.content, scope)

            logger.debug("Search returned %s results", len(results))
            return results

        except HTTPStatusError as e:
            # Surface the server's actual reason: Nextcloud/Sabre returns an XML
            # error body (e.g. "<s:message>...</s:message>") that pinpoints the
            # cause — far more actionable than the generic httpx
            # "Client error '400 Bad Request' for url '.../dav/'" string. A 400
            # here almost always means a malformed SEARCH body (commonly an
            # un-escaped character in the scope path).
            detail = (e.response.text or "").strip().replace("\n", " ")
            logger.error(
                "WebDAV SEARCH failed: HTTP %s for scope %r — %s",
                e.response.status_code,
                scope or "<user root>",
                detail[:500] or "(empty response body)",
            )
            raise e
        except Exception as e:
            logger.error(
                "Unexpected error during WebDAV SEARCH (scope %r): %s", scope, e
            )
            raise e

    async def search_files_all(
        self,
        scope: str = "",
        where_conditions: Optional[str] = None,
        properties: Optional[List[str]] = None,
        order_by: Optional[List[Tuple[str, str]]] = None,
        page_size: int = WEBDAV_SEARCH_PAGE_SIZE,
        max_results: int = WEBDAV_SEARCH_MAX_RESULTS,
    ) -> List[Dict[str, Any]]:
        """Fetch the *complete* SEARCH result set, paging past the server default.

        A plain ``search_files`` with no ``limit`` returns only Nextcloud's default
        page (~100), silently dropping the rest of a large folder. This method pages
        with ``<d:firstresult>`` until a short page signals the end. If the server
        ignores the offset (a page repeats results already seen), it falls back to a
        single fetch with an explicit large ``nresults`` so completeness never depends
        on offset support.

        Args:
            scope: Directory path to search in (empty string for user root)
            where_conditions: XML where-clause conditions
            properties: Properties to retrieve (must include ``fileid`` for dedup)
            order_by: Optional sort order
            page_size: Results requested per page
            max_results: Hard ceiling; crossing it logs a truncation warning and
                increments ``webdav_search_truncated_total``

        Returns:
            All matching file/directory dicts, de-duplicated by file id / path.
        """
        paged = await self._search_offset_paged(
            scope, where_conditions, properties, order_by, page_size, max_results
        )
        # ``None`` signals the server ignored the offset (or an offset page
        # failed) -- fetch everything in one bounded request instead.
        if paged is None:
            return await self._single_fetch_fallback(
                scope, where_conditions, properties, order_by, max_results
            )
        self._warn_if_truncated(len(paged), scope, max_results)
        return paged[:max_results]

    async def _search_offset_paged(
        self,
        scope: str,
        where_conditions: Optional[str],
        properties: Optional[List[str]],
        order_by: Optional[List[Tuple[str, str]]],
        page_size: int,
        max_results: int,
    ) -> Optional[List[Dict[str, Any]]]:
        """Page the SEARCH with ``<d:firstresult>`` until exhausted.

        Returns the accumulated rows, or ``None`` when the server ignores the
        offset (a page repeats already-seen rows, or an offset page errors) and
        the caller should fall back to a single bounded fetch.
        """

        def _key(item: Dict[str, Any]) -> Any:
            # file_id is globally unique; path is the stable fallback when a
            # producer omits fileid. ``id(item)`` is a last resort so an item
            # missing both never collapses into another under a shared ``None``
            # key (which would silently drop rows from the result set).
            # ``is not None`` rather than truthiness so a (hypothetical)
            # file_id of 0 isn't treated as absent.
            file_id = item.get("file_id")
            if file_id is not None:
                return file_id
            return item.get("path") or id(item)

        results: List[Dict[str, Any]] = []
        seen: set[Any] = set()
        offset = 0

        while len(results) < max_results:
            try:
                page = await self.search_files(
                    scope=scope,
                    where_conditions=where_conditions,
                    properties=properties,
                    order_by=order_by,
                    limit=page_size,
                    offset=offset,
                )
            except Exception:
                # A failure on the very first page is a real error, not a
                # paging quirk -- surface it. A later page failing means offset
                # paging is unusable; signal a fallback rather than lose the tail.
                if offset == 0:
                    raise
                logger.warning(
                    "WebDAV SEARCH offset page failed for scope %r; "
                    "falling back to single fetch",
                    scope,
                )
                return None

            if not page:
                break

            fresh = [item for item in page if _key(item) not in seen]

            # Server ignored the offset (returned an already-seen page); signal
            # the caller to re-fetch in one bounded request. The accumulated
            # ``results`` are intentionally discarded -- the single fetch is
            # authoritative and re-returns them, so nothing is lost.
            if offset > 0 and not fresh:
                logger.warning(
                    "WebDAV SEARCH ignored offset for scope %r; "
                    "falling back to single fetch (limit=%d)",
                    scope,
                    max_results,
                )
                return None

            for item in fresh:
                seen.add(_key(item))
                results.append(item)

            # A short page means we've reached the end of the result set.
            if len(page) < page_size:
                break

            offset += page_size

        return results

    async def _single_fetch_fallback(
        self,
        scope: str,
        where_conditions: Optional[str],
        properties: Optional[List[str]],
        order_by: Optional[List[Tuple[str, str]]],
        max_results: int,
    ) -> List[Dict[str, Any]]:
        """Single SEARCH with a large explicit ``nresults`` (offset-free fallback)."""
        results = await self.search_files(
            scope=scope,
            where_conditions=where_conditions,
            properties=properties,
            order_by=order_by,
            limit=max_results,
        )
        self._warn_if_truncated(len(results), scope, max_results)
        return results

    @staticmethod
    def _warn_if_truncated(count: int, scope: str, max_results: int) -> None:
        """Warn + count when a SEARCH hit the ceiling, so a cap is never silent."""
        if count >= max_results:
            document_scan_truncated_total.inc()
            logger.warning(
                "WebDAV SEARCH reached max_results=%d for scope %r; "
                "results may be truncated -- raise WEBDAV_SEARCH_MAX_RESULTS",
                max_results,
                scope,
            )

    def _build_search_xml(
        self,
        scope: str,
        where_conditions: Optional[str],
        properties: List[str],
        order_by: Optional[List[Tuple[str, str]]],
        limit: Optional[int],
        offset: Optional[int] = None,
    ) -> str:
        """Build the XML body for a SEARCH request."""
        # Construct the scope path
        principal = self._principal_or_username()
        scope_path = f"/files/{principal}"
        if scope:
            scope_path = f"{scope_path}/{scope.lstrip('/')}"
        # XML-escape before embedding in <d:href>: a folder whose name contains
        # '&', '<' or '>' (e.g. "Reports & Plans") otherwise produces a malformed
        # SEARCH body that Nextcloud's Sabre/DAV parser rejects with 400 Bad
        # Request — silently skipping that folder and all of its descendants
        # during the tag-based indexing walk. Escaping keeps the path literal
        # (Sabre unescapes it back), matching how folders without special
        # characters already resolve.
        scope_href = xml_escape(scope_path)

        # Build property list
        prop_xml = "\n".join([self._property_to_xml(prop) for prop in properties])

        # Build where clause. An *empty* ``<d:where>`` is not a match-all:
        # Nextcloud answers it with ``500`` and a Sabre ``TypeError`` document
        # (verified against 32.0.14). So an unfiltered search -- "everything
        # under this folder", the most obvious call an MCP client can make --
        # needs an explicit predicate that holds for every row instead.
        # ``displayname LIKE '%'`` is that predicate, and reuses the operator
        # the filtered callers already build.
        where_xml = (where_conditions or "").strip() or (
            "<d:like><d:prop><d:displayname/></d:prop><d:literal>%</d:literal></d:like>"
        )

        # Build order by clause
        orderby_xml = ""
        if order_by:
            order_elements = []
            for prop, direction in order_by:
                prop_element = self._property_to_xml(prop)
                dir_element = (
                    "<d:ascending/>"
                    if direction.lower() == "ascending"
                    else "<d:descending/>"
                )
                order_elements.append(f"<d:order>{prop_element}{dir_element}</d:order>")
            orderby_xml = "\n".join(order_elements)
        else:
            orderby_xml = ""

        # Build limit clause. ``<d:nresults>`` caps the page size; ``<d:firstresult>``
        # is the paging offset. Nextcloud silently ignores an unsupported offset
        # (returns the first page again) rather than erroring -- ``search_files_all``
        # detects that non-progress and falls back to a single bounded fetch.
        limit_parts = []
        if limit:
            limit_parts.append(f"<d:nresults>{limit}</d:nresults>")
        # ``is not None`` (not truthiness) so a future explicit offset=0 is
        # emitted rather than silently dropped.
        if offset is not None:
            limit_parts.append(f"<d:firstresult>{offset}</d:firstresult>")
        limit_xml = f"<d:limit>{''.join(limit_parts)}</d:limit>" if limit_parts else ""

        # Construct the full SEARCH XML
        search_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
    <d:basicsearch>
        <d:select>
            <d:prop>
                {prop_xml}
            </d:prop>
        </d:select>
        <d:from>
            <d:scope>
                <d:href>{scope_href}</d:href>
                <d:depth>infinity</d:depth>
            </d:scope>
        </d:from>
        <d:where>
            {where_xml}
        </d:where>
        <d:orderby>
            {orderby_xml}
        </d:orderby>
        {limit_xml}
    </d:basicsearch>
</d:searchrequest>"""

        return search_xml

    def _property_to_xml(self, prop: str) -> str:
        """Convert a property name to its XML element."""
        # Handle properties with namespace prefixes
        if prop.startswith("{"):
            # Already a full namespace
            namespace_end = prop.index("}")
            namespace = prop[1:namespace_end]
            local_name = prop[namespace_end + 1 :]

            # Map namespace URIs to prefixes
            ns_map = {
                "DAV:": "d",
                "http://owncloud.org/ns": "oc",
                "http://nextcloud.org/ns": "nc",
            }

            prefix = ns_map.get(namespace, "d")
            return f"<{prefix}:{local_name}/>"
        else:
            # Guess namespace based on common properties
            if prop in [
                "displayname",
                "getcontentlength",
                "getcontenttype",
                "getlastmodified",
                "resourcetype",
                "getetag",
                "quota-available-bytes",
                "quota-used-bytes",
            ]:
                return f"<d:{prop}/>"
            elif prop in [
                "fileid",
                "size",
                "permissions",
                "favorite",
                "tags",
                "owner-id",
                "owner-display-name",
                "share-types",
                "checksums",
                "comments-count",
                "comments-unread",
            ]:
                return f"<oc:{prop}/>"
            else:
                # Assume nc namespace for newer properties
                return f"<nc:{prop}/>"

    def _parse_search_response(
        self, xml_content: bytes, scope: str
    ) -> List[Dict[str, Any]]:
        """Parse the XML response from a SEARCH request."""
        root = ET.fromstring(xml_content)
        items = []

        # Process each response element
        responses = root.findall(".//{DAV:}response")

        for response_elem in responses:
            href = response_elem.find(".//{DAV:}href")
            if href is None:
                continue

            # Extract file/directory path from href. <d:href> is required by
            # RFC 3986 to be percent-encoded, so non-ASCII paths arrive
            # encoded — decode before exposing to callers (issue #776).
            href_text = unquote(href.text or "")
            # Remove the /remote.php/dav/files/<principal>/ prefix to get relative path.
            path_parts = href_text.split("/files/")
            if len(path_parts) > 1:
                # Get the path after the principal segment.
                path_after_user = "/".join(path_parts[1].split("/")[1:])
                relative_path = path_after_user.rstrip("/")
            else:
                relative_path = href_text.rstrip("/").split("/")[-1]

            # Get properties
            propstat = response_elem.find(".//{DAV:}propstat")
            if propstat is None:
                continue

            prop = propstat.find(".//{DAV:}prop")
            if prop is None:
                continue

            # Build item dictionary
            item = {"path": relative_path, "href": href_text}

            # Extract all properties
            for child in prop:
                tag = child.tag
                value = child.text

                # Remove namespace from tag
                if "}" in tag:
                    tag = tag.split("}", 1)[1]

                # Handle special properties
                if tag == "resourcetype":
                    item["is_directory"] = child.find(".//{DAV:}collection") is not None
                elif tag == "getcontentlength":
                    item["size"] = int(value) if value else 0
                elif tag == "displayname":
                    item["name"] = value
                elif tag == "getcontenttype":
                    item["content_type"] = value
                elif tag == "getlastmodified":
                    item["last_modified"] = value
                elif tag == "getetag":
                    item["etag"] = _normalize_etag(value) if value else None
                elif tag == "fileid":
                    item["file_id"] = int(value) if value else None
                elif tag == "favorite":
                    item["is_favorite"] = value == "1"
                elif tag == "tags":
                    # Tags can be comma-separated or have multiple child elements
                    if value:
                        # Handle comma-separated tags
                        item["tags"] = [
                            t.strip() for t in value.split(",") if t.strip()
                        ]
                    else:
                        # Check for child tag elements (alternative format)
                        tag_elements = child.findall(".//{http://owncloud.org/ns}tag")
                        if tag_elements:
                            item["tags"] = [t.text for t in tag_elements if t.text]
                        else:
                            item["tags"] = []
                elif tag == "permissions":
                    item["permissions"] = value
                elif tag == "size":
                    # oc:size includes folder sizes
                    item["total_size"] = int(value) if value else 0
                else:
                    # Store other properties as-is
                    item[tag] = value

            items.append(item)

        return items

    async def find_by_name(
        self, pattern: str, scope: str = "", limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Find files by name pattern using LIKE matching.

        Args:
            pattern: Name pattern to search for (supports % wildcard)
            scope: Directory path to search in (empty string for user root)
            limit: Maximum number of results to return

        Returns:
            List of matching files/directories

        Examples:
            # Find all .txt files
            results = await find_by_name("%.txt")

            # Find files starting with "report"
            results = await find_by_name("report%")
        """
        where_conditions = like_predicate("d:displayname", pattern)

        return await self.search_files(
            scope=scope, where_conditions=where_conditions, limit=limit
        )

    async def find_by_type(
        self, mime_type: str, scope: str = "", limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Find files by MIME type.

        Args:
            mime_type: MIME type to search for (supports % wildcard, e.g., "image/%")
            scope: Directory path to search in (empty string for user root)
            limit: Maximum number of results to return

        Returns:
            List of matching files

        Examples:
            # Find all images
            results = await find_by_type("image/%")

            # Find all PDFs
            results = await find_by_type("application/pdf")

        Note:
            With ``limit=None`` this returns only Nextcloud's default SEARCH page
            (~100 results), so it truncates large folders. Use ``find_all_by_type``
            when complete coverage matters (e.g. building an indexing work-list).
        """
        where_conditions, properties = self._type_search_args(mime_type)
        return await self.search_files(
            scope=scope,
            where_conditions=where_conditions,
            properties=properties,
            limit=limit,
        )

    async def find_all_by_type(
        self, mime_type: str, scope: str = ""
    ) -> List[Dict[str, Any]]:
        """Find *all* files of a MIME type, paging past the SEARCH default page.

        Unlike ``find_by_type`` (single default-capped page), this pages the SEARCH
        to completion so a large tagged folder is fully discovered. Used by the
        vector-sync scanner's tagged-folder expansion, where a missed file means a
        document that is never indexed.

        Args:
            mime_type: MIME type to search for (supports % wildcard)
            scope: Directory path to search in (empty string for user root)

        Returns:
            All matching files (bounded by ``WEBDAV_SEARCH_MAX_RESULTS``).
        """
        where_conditions, properties = self._type_search_args(mime_type)
        return await self.search_files_all(
            scope=scope,
            where_conditions=where_conditions,
            properties=properties,
        )

    @staticmethod
    def _type_search_args(mime_type: str) -> Tuple[str, List[str]]:
        """Build the where-clause + property list for a MIME-type SEARCH."""
        # Escape so a caller-supplied MIME type can't break the SEARCH XML or
        # inject elements. All current callers pass literal strings, but this
        # keeps the boundary safe for any future user-supplied value.
        where_conditions = like_predicate("d:getcontenttype", mime_type)

        # fileid is required by callers like NextcloudClient.find_files_by_tag
        # that dedupe results by id; the default property set in search_files
        # omits it.
        properties = [
            "displayname",
            "getcontentlength",
            "getcontenttype",
            "getlastmodified",
            "resourcetype",
            "getetag",
            "fileid",
        ]
        return where_conditions, properties

    async def list_favorites(
        self, scope: str = "", limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """List all favorite files.

        Args:
            scope: Directory path to search in (empty string for user root)
            limit: Maximum number of results to return

        Returns:
            List of favorite files/directories

        Examples:
            # List all favorites
            results = await list_favorites()

            # List favorites in a specific folder
            results = await list_favorites(scope="Documents")
        """
        # Use REPORT method for favorites as it's more efficient
        # But we can also use SEARCH as fallback
        where_conditions = """
            <d:eq>
                <d:prop>
                    <oc:favorite/>
                </d:prop>
                <d:literal>1</d:literal>
            </d:eq>
        """

        # Request favorite property
        properties = [
            "displayname",
            "getcontentlength",
            "getcontenttype",
            "getlastmodified",
            "resourcetype",
            "getetag",
            "fileid",
            "favorite",
        ]

        return await self.search_files(
            scope=scope,
            where_conditions=where_conditions,
            properties=properties,
            limit=limit,
        )

    async def find_by_tag(
        self, tag_name: str, scope: str = "", limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Find files by tag name.

        DEPRECATED: Use NextcloudClient.find_files_by_tag() instead, which uses
        the proper OCS Tags API rather than WebDAV SEARCH.

        Args:
            tag_name: Tag to filter by (e.g., "vector-index")
            scope: Directory path to search in (empty string for user root)
            limit: Maximum number of results to return

        Returns:
            List of files/directories with the specified tag

        Examples:
            # Find all files tagged with "vector-index"
            results = await find_by_tag("vector-index")

            # Find tagged files in a specific folder
            results = await find_by_tag("vector-index", scope="Documents")
        """
        # Use LIKE for tag matching since tags can be comma-separated
        where_conditions = like_predicate("oc:tags", f"%{tag_name}%")

        # Request tag property along with standard properties
        properties = [
            "displayname",
            "getcontentlength",
            "getcontenttype",
            "getlastmodified",
            "resourcetype",
            "getetag",
            "fileid",
            "tags",
        ]

        return await self.search_files(
            scope=scope,
            where_conditions=where_conditions,
            properties=properties,
            limit=limit,
        )

    async def file_accessible_by_id(self, file_id: int) -> bool:
        """ACL-aware access check for a file by its global Nextcloud file ID.

        Used by verify-on-read (ADR-019). Searches the authenticated user's
        whole files tree — which *includes mounted shares* — via WebDAV SEARCH
        (RFC 5323) filtered on ``oc:fileid``, returning True iff the user can
        currently access the file.

        This is the only check that resolves shared files correctly:

        - :meth:`get_file_info` resolves a path under the caller's *own* root,
          so it 404s on a file shared into the caller's account (Nextcloud
          mounts received shares at the recipient's root by basename, a
          different path than the owner indexed).
        - The ``/remote.php/dav/meta/{id}/`` endpoint resolves only the user's
          *own* storage, so it 404s on shared files too.

        SEARCH-by-fileid handles all cases: owned files, directly-shared files,
        and files reachable via a shared parent folder (verified empirically).

        Args:
            file_id: Nextcloud internal (global) file ID.

        Returns:
            True if the user can access the file, False if it is not present
            in their tree (not owned and not shared with them).

        Raises:
            HTTPStatusError: On transport/server errors — callers treat these
                as transient (keep the result), not as a definitive denial.
        """
        where = (
            "<d:eq><d:prop><oc:fileid/></d:prop>"
            f"<d:literal>{int(file_id)}</d:literal></d:eq>"
        )
        results = await self.search_files(
            scope="",  # user's whole files tree, incl. mounted shares
            where_conditions=where,
            properties=["fileid"],
            limit=1,
        )
        return len(results) > 0

    async def get_fileid(self, path: str) -> str | None:
        """Return the Nextcloud fileid of a file/folder path, or None if absent.

        A Depth-0 ``PROPFIND`` for ``oc:fileid`` on the principal-scoped path.
        Used by the folder-ancestor resolver (ADR-033 Phase 3) to map an ancestor
        folder path to its canonical fileid — stable across every user who mounts
        a shared folder, which is what makes the folder-scope search filter
        user-agnostic. A 404 (path gone/inaccessible) returns None so callers
        degrade gracefully rather than aborting an index.
        """
        await self._ensure_principal_id()
        webdav_path = self._webdav_path(path)
        propfind_body = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            "<d:prop><oc:fileid/></d:prop></d:propfind>"
        )
        headers = {"Depth": "0", "Content-Type": "text/xml", "OCS-APIRequest": "true"}
        try:
            response = await self._make_request(
                "PROPFIND", webdav_path, content=propfind_body, headers=headers
            )
            response.raise_for_status()
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        root = ET.fromstring(response.content)
        # Match oc:fileid by local name (namespace-agnostic) rather than a
        # namespace map, so the owncloud namespace URL is not embedded as a
        # standalone string literal (it stays only inside the request-body XML).
        for elem in root.iter():
            if isinstance(elem.tag, str) and elem.tag.rsplit("}", 1)[-1] == "fileid":
                text = (elem.text or "").strip()
                if text:
                    return text
        return None

    async def list_comments(
        self, file_id: int, *, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return comments on a file, newest first.

        A ``REPORT`` carrying ``oc:filter-comments`` rather than a ``PROPFIND``:
        the report is the only form that takes ``limit``/``offset``, and a
        Depth-1 PROPFIND on the collection would return the entire thread.

        Args:
            file_id: Nextcloud file ID (see :meth:`get_fileid`).
            limit: Maximum comments to return.
            offset: How many of the newest comments to skip.

        Returns:
            One dict per comment, in the shape ``FileComment`` expects.
        """
        # The owncloud namespace stays inside the request body, never a
        # standalone string constant -- same reason as get_fileid, which matches
        # oc properties by local name rather than naming the URL (see
        # _dav_props_ok). A bare URL literal also trips Sonar's python:S5332.
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<oc:filter-comments xmlns:oc="http://owncloud.org/ns">'
            f"<oc:limit>{int(limit)}</oc:limit>"
            f"<oc:offset>{int(offset)}</oc:offset>"
            "</oc:filter-comments>"
        )
        response = await self._make_request(
            "REPORT",
            f"{_COMMENTS_PATH}/{int(file_id)}",
            content=body,
            headers={
                "Depth": "0",
                "Content-Type": "text/xml",
                "OCS-APIRequest": "true",
            },
        )

        root = ET.fromstring(response.content)
        comments = []
        for response_elem in root.findall("{DAV:}response"):
            props = _dav_props_ok(response_elem)
            if not props:
                continue
            comment = _parse_comment_props(props)
            if comment is not None:
                comments.append(comment)

        logger.debug("Found %s comment(s) on file %s", len(comments), file_id)
        return comments

    async def create_comment(self, file_id: int, message: str) -> int | None:
        """Post a comment on a file and return the new comment's ID.

        Nextcloud answers ``201`` with an **empty body**, naming the new comment
        only in the ``Content-Location`` header — so the ID is read from there
        rather than by fetching the comment back.

        Mentions are the server's business: it parses ``@"username"`` out of the
        message when storing it and sends the notification itself.

        Args:
            file_id: Nextcloud file ID (see :meth:`get_fileid`).
            message: Comment text. The caller is expected to have checked it
                against Nextcloud's 1000-character limit.

        Returns:
            The new comment's ID, or None if the server named no location.
        """
        response = await self._make_request(
            "POST",
            f"{_COMMENTS_PATH}/{int(file_id)}",
            json={"actorType": "users", "verb": "comment", "message": message},
            headers={"OCS-APIRequest": "true"},
        )

        location = response.headers.get("Content-Location", "")
        comment_id = location.rstrip("/").rsplit("/", 1)[-1]
        try:
            return int(comment_id)
        except ValueError:
            logger.warning(
                "Comment created on file %s but Content-Location %r carries no id",
                file_id,
                location,
            )
            return None

    async def _get_file_info_by_id(self, file_id: int) -> Dict[str, Any]:
        """Get file information by Nextcloud file ID using WebDAV.

        Args:
            file_id: Nextcloud internal file ID

        Returns:
            File information dictionary with path, size, content_type, etc.

        Raises:
            HTTPStatusError: If file not found or request fails
        """
        # Nextcloud allows accessing files by ID via special meta endpoint
        meta_path = f"/remote.php/dav/meta/{file_id}/"

        propfind_body = """<?xml version="1.0"?>
        <d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
            <d:prop>
                <d:displayname/>
                <d:getcontentlength/>
                <d:getcontenttype/>
                <d:getlastmodified/>
                <d:resourcetype/>
                <d:getetag/>
                <oc:fileid/>
            </d:prop>
        </d:propfind>"""

        headers = {"Depth": "0", "Content-Type": "text/xml", "OCS-APIRequest": "true"}

        response = await self._make_request(
            "PROPFIND", meta_path, content=propfind_body, headers=headers
        )
        response.raise_for_status()

        # Parse the XML response
        root = ET.fromstring(response.content)
        responses = root.findall(".//{DAV:}response")

        if not responses:
            raise RuntimeError(f"File ID {file_id} not found")

        response_elem = responses[0]
        href = response_elem.find(".//{DAV:}href")
        if href is None:
            raise RuntimeError(f"No href in response for file ID {file_id}")

        propstat = response_elem.find(".//{DAV:}propstat")
        if propstat is None:
            raise RuntimeError(f"No propstat for file ID {file_id}")

        prop = propstat.find(".//{DAV:}prop")
        if prop is None:
            raise RuntimeError(f"No prop for file ID {file_id}")

        # Extract file path from displayname or construct from file ID
        displayname_elem = prop.find(".//{DAV:}displayname")
        name = (
            displayname_elem.text if displayname_elem is not None else f"file_{file_id}"
        )

        # Get file properties
        size_elem = prop.find(".//{DAV:}getcontentlength")
        size = int(size_elem.text) if size_elem is not None and size_elem.text else 0

        content_type_elem = prop.find(".//{DAV:}getcontenttype")
        content_type = content_type_elem.text if content_type_elem is not None else None

        modified_elem = prop.find(".//{DAV:}getlastmodified")
        modified = modified_elem.text if modified_elem is not None else None

        etag_elem = prop.find(".//{DAV:}getetag")
        etag = (
            _normalize_etag(etag_elem.text)
            if etag_elem is not None and etag_elem.text
            else None
        )

        # Check if it's a directory
        resourcetype = prop.find(".//{DAV:}resourcetype")
        is_directory = (
            resourcetype is not None
            and resourcetype.find(".//{DAV:}collection") is not None
        )

        # Try to get actual file path - meta endpoint doesn't give us the real path
        # so we'll construct a reasonable path from the name
        # The calling code in NextcloudClient will have the context to determine the actual path
        file_info = {
            "name": name,
            "path": f"/{name}",  # Placeholder - caller should use WebDAV to get real path if needed
            "size": size,
            "content_type": content_type,
            "last_modified": modified,
            "etag": etag,
            "is_directory": is_directory,
            "file_id": file_id,
        }

        logger.debug("Retrieved file info for ID %s: %s", file_id, name)
        return file_info

    async def get_tag_by_name(self, tag_name: str) -> dict[str, Any] | None:
        """Get a system tag by its name via WebDAV.

        Args:
            tag_name: Name of the tag to find (case-sensitive)

        Returns:
            Tag dictionary if found, None otherwise
        """
        # Use WebDAV PROPFIND to list all systemtags
        propfind_body = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <oc:id/>
    <oc:display-name/>
    <oc:user-visible/>
    <oc:user-assignable/>
  </d:prop>
</d:propfind>"""

        response = await self._make_request(
            "PROPFIND",
            "/remote.php/dav/systemtags/",
            headers={
                "Depth": "1",
                "Content-Type": "text/xml",
                "OCS-APIRequest": "true",
            },
            content=propfind_body,
        )
        # Redundant after _make_request (which raises on non-2xx) but
        # makes the contract explicit at the call site so a future
        # refactor of _make_request cannot silently feed an error body
        # into ET.fromstring below.
        response.raise_for_status()

        # Parse XML response
        root = ET.fromstring(response.content)
        ns = {
            "d": "DAV:",
            "oc": "http://owncloud.org/ns",
        }

        for response_elem in root.findall("d:response", ns):
            href = response_elem.find("d:href", ns)
            if href is None or href.text == "/remote.php/dav/systemtags/":
                # Skip the collection itself
                continue

            propstat = response_elem.find("d:propstat", ns)
            if propstat is None:
                continue

            prop = propstat.find("d:prop", ns)
            if prop is None:
                continue

            # Extract tag properties
            tag_id_elem = prop.find("oc:id", ns)
            display_name_elem = prop.find("oc:display-name", ns)
            user_visible_elem = prop.find("oc:user-visible", ns)
            user_assignable_elem = prop.find("oc:user-assignable", ns)

            if display_name_elem is not None and display_name_elem.text == tag_name:
                tag_info = {
                    "id": int(tag_id_elem.text)
                    if tag_id_elem is not None and tag_id_elem.text is not None
                    else None,
                    "name": display_name_elem.text,
                    "userVisible": user_visible_elem.text.lower() == "true"
                    if user_visible_elem is not None
                    and user_visible_elem.text is not None
                    else True,
                    "userAssignable": user_assignable_elem.text.lower() == "true"
                    if user_assignable_elem is not None
                    and user_assignable_elem.text is not None
                    else True,
                }
                logger.debug("Found tag %r with ID %s", tag_name, tag_info["id"])
                return tag_info

        logger.debug("Tag %r not found", tag_name)
        return None

    async def get_files_by_tag(self, tag_id: int) -> list[dict[str, Any]]:
        """Get all files tagged with a specific system tag via WebDAV REPORT.

        Args:
            tag_id: Numeric ID of the tag

        Returns:
            List of file info dictionaries with path, size, content_type, etc.
        """
        await self._ensure_principal_id()
        # Use WebDAV REPORT method with systemtag filter. resourcetype is
        # included so callers can distinguish folders from files (needed for
        # recursive exclusion of tagged directories — see issue #710).
        report_body = f"""<?xml version="1.0"?>
<oc:filter-files xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <oc:fileid/>
    <d:displayname/>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <d:getlastmodified/>
    <d:getetag/>
    <d:resourcetype/>
  </d:prop>
  <oc:filter-rules>
    <oc:systemtag>{tag_id}</oc:systemtag>
  </oc:filter-rules>
</oc:filter-files>"""

        response = await self._make_request(
            "REPORT",
            f"{self._get_webdav_base_path()}/",
            headers={"Content-Type": "text/xml", "OCS-APIRequest": "true"},
            content=report_body,
        )
        # Redundant after _make_request (which raises on non-2xx) but
        # makes the contract explicit at the call site — see the same
        # rationale in get_tag_by_name.
        response.raise_for_status()

        # Parse XML response
        root = ET.fromstring(response.content)
        ns = {
            "d": "DAV:",
            "oc": "http://owncloud.org/ns",
        }

        files = []
        for response_elem in root.findall("d:response", ns):
            # Extract href (file path)
            href_elem = response_elem.find("d:href", ns)
            if href_elem is None or not href_elem.text:
                continue

            propstat = response_elem.find("d:propstat", ns)
            if propstat is None:
                continue

            prop = propstat.find("d:prop", ns)
            if prop is None:
                continue

            # Extract all properties
            fileid_elem = prop.find("oc:fileid", ns)
            displayname_elem = prop.find("d:displayname", ns)
            contentlength_elem = prop.find("d:getcontentlength", ns)
            contenttype_elem = prop.find("d:getcontenttype", ns)
            lastmodified_elem = prop.find("d:getlastmodified", ns)
            etag_elem = prop.find("d:getetag", ns)
            resourcetype_elem = prop.find("d:resourcetype", ns)

            if fileid_elem is None or not fileid_elem.text:
                continue

            # A resourcetype with a <d:collection/> child indicates a folder.
            is_directory = (
                resourcetype_elem is not None
                and resourcetype_elem.find("d:collection", ns) is not None
            )

            # Decode href path and extract the user-relative file path.
            # str.replace() would strip every occurrence of the prefix,
            # so an adversarially-named directory could collide; strip
            # only the leading occurrence via startswith + slice.
            href_path = unquote(href_elem.text)
            webdav_prefix = f"/remote.php/dav/files/{self._principal_or_username()}/"
            if href_path.startswith(webdav_prefix):
                file_path = "/" + href_path[len(webdav_prefix) :]
            else:
                file_path = href_path

            # Parse last modified timestamp
            last_modified_timestamp = None
            if lastmodified_elem is not None and lastmodified_elem.text:
                try:
                    dt = parsedate_to_datetime(lastmodified_elem.text)
                    last_modified_timestamp = int(dt.timestamp())
                except Exception:
                    pass

            file_info = {
                "id": int(fileid_elem.text),
                "path": file_path,
                "name": displayname_elem.text
                if displayname_elem is not None
                else file_path.split("/")[-1],
                "size": int(contentlength_elem.text)
                if contentlength_elem is not None and contentlength_elem.text
                else 0,
                "content_type": contenttype_elem.text
                if contenttype_elem is not None
                else "",
                "last_modified": lastmodified_elem.text
                if lastmodified_elem is not None
                else None,
                "last_modified_timestamp": last_modified_timestamp,
                "etag": etag_elem.text if etag_elem is not None else None,
                "is_directory": is_directory,
            }
            files.append(file_info)

        logger.debug("Found %d files with tag ID %s", len(files), tag_id)
        return files

    async def get_file_info(self, path: str) -> dict[str, Any] | None:
        """Get file info including file ID via WebDAV PROPFIND.

        .. note::
            **Behavior change (ADR-019):** previously this method returned
            ``None`` for HTTP 404. It now raises ``HTTPStatusError`` for any
            non-2xx status, including 404. ``None`` is reserved for the
            ambiguous *malformed PROPFIND* case (server returned 2xx with a
            response body missing required XML elements). External callers
            updating from the old contract must catch ``HTTPStatusError``
            and inspect ``e.response.status_code`` to handle 404 explicitly.

        Args:
            path: Path to the file (relative to user's files directory)

        Returns:
            File info dictionary with id, name, size, content_type, etc.
            Returns ``None`` ONLY when the server returned a malformed
            PROPFIND response (missing ``<d:response>`` /
            ``<d:propstat>`` / ``<d:prop>`` elements) — an ambiguous
            state where we cannot tell whether the file exists.

        Raises:
            HTTPStatusError: For any non-2xx HTTP status, including 404
                ("not found"). Callers that want to treat 404 as
                "absent" should catch ``HTTPStatusError`` and check
                ``e.response.status_code``. This matches the convention
                of the rest of this client and lets verify-on-read
                distinguish a definitive absence (HTTP 404) from a
                brittle response (None).
        """
        await self._ensure_principal_id()
        webdav_path = self._webdav_path(path)

        propfind_body = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <oc:fileid/>
    <d:displayname/>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <d:getlastmodified/>
    <d:getetag/>
    <d:resourcetype/>
  </d:prop>
</d:propfind>"""

        response = await self._client.request(
            "PROPFIND",
            webdav_path,
            headers={"Depth": "0"},
            content=propfind_body,
        )
        response.raise_for_status()

        # Parse XML response
        root = ET.fromstring(response.content)
        ns = {
            "d": "DAV:",
            "oc": "http://owncloud.org/ns",
        }

        response_elem = root.find("d:response", ns)
        if response_elem is None:
            return None

        propstat = response_elem.find("d:propstat", ns)
        if propstat is None:
            return None

        prop = propstat.find("d:prop", ns)
        if prop is None:
            return None

        # Extract properties
        fileid_elem = prop.find("oc:fileid", ns)
        displayname_elem = prop.find("d:displayname", ns)
        contentlength_elem = prop.find("d:getcontentlength", ns)
        contenttype_elem = prop.find("d:getcontenttype", ns)
        lastmodified_elem = prop.find("d:getlastmodified", ns)
        etag_elem = prop.find("d:getetag", ns)
        resourcetype_elem = prop.find("d:resourcetype", ns)

        is_directory = (
            resourcetype_elem is not None
            and resourcetype_elem.find("d:collection", ns) is not None
        )

        file_info = {
            "id": int(fileid_elem.text)
            if fileid_elem is not None and fileid_elem.text is not None
            else None,
            "path": path,
            "name": displayname_elem.text
            if displayname_elem is not None
            else path.split("/")[-1],
            "size": int(contentlength_elem.text)
            if contentlength_elem is not None and contentlength_elem.text
            else 0,
            "content_type": contenttype_elem.text
            if contenttype_elem is not None
            else "",
            "last_modified": lastmodified_elem.text
            if lastmodified_elem is not None
            else None,
            "etag": _normalize_etag(etag_elem.text)
            if etag_elem is not None and etag_elem.text
            else None,
            "is_directory": is_directory,
        }

        logger.debug("Got file info for '%s': id=%s", path, file_info["id"])
        return file_info

    async def create_tag(
        self,
        name: str,
        user_visible: bool = True,
        user_assignable: bool = True,
    ) -> dict[str, Any]:
        """Create a system tag via WebDAV.

        Args:
            name: Name of the tag to create
            user_visible: Whether the tag is visible to users
            user_assignable: Whether users can assign this tag

        Returns:
            Tag dictionary with id, name, userVisible, userAssignable

        Raises:
            HTTPStatusError: If tag creation fails (409 if already exists)
        """
        # Use WebDAV POST with JSON body to create tag
        response = await self._client.post(
            "/remote.php/dav/systemtags/",
            headers={"Content-Type": "application/json"},
            json={
                "name": name,
                "userVisible": user_visible,
                "userAssignable": user_assignable,
            },
        )
        response.raise_for_status()

        # Extract tag ID from Content-Location header (e.g., /remote.php/dav/systemtags/42)
        content_location = response.headers.get("Content-Location", "")
        tag_id = None
        if content_location:
            # Extract the numeric ID from the path
            try:
                tag_id = int(content_location.rstrip("/").split("/")[-1])
            except (ValueError, IndexError):
                pass

        tag_info = {
            "id": tag_id,
            "name": name,
            "userVisible": user_visible,
            "userAssignable": user_assignable,
        }

        logger.info("Created tag '%s' with ID %s", name, tag_info["id"])
        return tag_info

    async def get_or_create_tag(
        self,
        name: str,
        user_visible: bool = True,
        user_assignable: bool = True,
    ) -> dict[str, Any]:
        """Get a tag by name, creating it if it doesn't exist.

        Args:
            name: Name of the tag
            user_visible: Whether the tag is visible to users (for creation)
            user_assignable: Whether users can assign this tag (for creation)

        Returns:
            Tag dictionary with id, name, userVisible, userAssignable
        """
        # First try to get existing tag
        existing_tag = await self.get_tag_by_name(name)
        if existing_tag:
            logger.debug("Tag '%s' already exists with ID %s", name, existing_tag["id"])
            return existing_tag

        # Create new tag
        try:
            return await self.create_tag(name, user_visible, user_assignable)
        except HTTPStatusError as e:
            if e.response.status_code == 409:
                # Tag was created between our check and creation, fetch it
                existing_tag = await self.get_tag_by_name(name)
                if existing_tag:
                    return existing_tag
            raise

    async def assign_tag_to_file(self, file_id: int, tag_id: int) -> bool:
        """Assign a system tag to a file.

        Args:
            file_id: Numeric file ID
            tag_id: Numeric tag ID

        Returns:
            True if tag was assigned successfully (or already assigned)

        Raises:
            HTTPStatusError: If tag assignment fails
        """
        response = await self._client.request(
            "PUT",
            f"/remote.php/dav/systemtags-relations/files/{file_id}/{tag_id}",
            headers={"Content-Length": "0"},
            content=b"",
        )

        # 201 = Created (new assignment), 409 = Conflict (already assigned)
        if response.status_code in (201, 409):
            logger.info("Tagged file %s with tag %s", file_id, tag_id)
            return True

        response.raise_for_status()
        return True

    async def remove_tag_from_file(self, file_id: int, tag_id: int) -> bool:
        """Remove a system tag from a file.

        Args:
            file_id: Numeric file ID
            tag_id: Numeric tag ID

        Returns:
            True if tag was removed successfully (or wasn't assigned)

        Raises:
            HTTPStatusError: If tag removal fails
        """
        response = await self._client.request(
            "DELETE",
            f"/remote.php/dav/systemtags-relations/files/{file_id}/{tag_id}",
        )

        # 204 = No Content (removed), 404 = Not Found (wasn't assigned)
        if response.status_code in (204, 404):
            logger.info("Removed tag %s from file %s", tag_id, file_id)
            return True

        response.raise_for_status()
        return True

    # -- File tags (systemtags) ---------------------------------------------
    #
    # The client already covered tags (create/assign/remove/search); only two
    # read paths were missing: list every tag, and list the tags of one file.

    _TAG_PROPFIND = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <oc:id/>
    <oc:display-name/>
    <oc:user-visible/>
    <oc:user-assignable/>
  </d:prop>
</d:propfind>"""

    async def list_tags(self) -> List[Dict[str, Any]]:
        """Every system tag defined on this instance."""
        response = await self._make_request(
            "PROPFIND",
            "/remote.php/dav/systemtags/",
            headers={
                "Depth": "1",
                "Content-Type": "text/xml",
                "OCS-APIRequest": "true",
            },
            content=self._TAG_PROPFIND,
        )

        return self._tags_from_multistatus(ET.fromstring(response.content))

    @staticmethod
    def _tags_from_multistatus(root: Any) -> List[Dict[str, Any]]:
        """Extract tags from a systemtags PROPFIND response.

        The collection itself comes back as a response element and is skipped.
        An entry without an id or display name cannot be used for anything, so
        it is dropped rather than surfaced with placeholder values.
        """
        tags: List[Dict[str, Any]] = []
        for response_elem in root.findall("{DAV:}response"):
            href = response_elem.find("{DAV:}href")
            if href is None or href.text == "/remote.php/dav/systemtags/":
                continue
            name_elem = response_elem.find(".//{http://owncloud.org/ns}display-name")
            id_elem = response_elem.find(".//{http://owncloud.org/ns}id")
            # An entry without a usable id or name cannot be acted on, and an
            # empty display name would also make the sort key ambiguous.
            if id_elem is None or not id_elem.text:
                continue
            if name_elem is None or not (name_elem.text or "").strip():
                continue
            assignable = response_elem.find(
                ".//{http://owncloud.org/ns}user-assignable"
            )
            tags.append(
                {
                    "id": int(id_elem.text),
                    "name": name_elem.text or "",
                    "assignable": (assignable is None or assignable.text != "false"),
                }
            )
        return sorted(tags, key=lambda t: t["name"].lower())

    async def get_file_tags(self, path: str) -> Dict[str, Any]:
        """The tags assigned to one file.

        Nextcloud only reports tag *ids* on the file, so the names are looked
        up from the full list. That is one extra call, but ids alone are of
        little use to the caller -- and unguessable for a language model.
        """
        file_id = await self.get_fileid(path)
        if not file_id:
            raise ValueError(f"No file id for path: {path}")

        response = await self._make_request(
            "PROPFIND",
            f"/remote.php/dav/systemtags-relations/files/{file_id}",
            headers={
                "Depth": "1",
                "Content-Type": "text/xml",
                "OCS-APIRequest": "true",
            },
            content=self._TAG_PROPFIND,
        )

        root = ET.fromstring(response.content)
        ids: List[int] = []
        for id_elem in root.findall(".//{http://owncloud.org/ns}id"):
            if id_elem.text and id_elem.text.isdigit():
                ids.append(int(id_elem.text))

        names = {t["id"]: t["name"] for t in await self.list_tags()}
        return {
            "path": path,
            "file_id": file_id,
            "tags": [
                {"id": i, "name": names.get(i, f"(unknown tag {i})")}
                for i in sorted(set(ids))
            ],
        }
