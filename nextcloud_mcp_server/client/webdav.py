"""WebDAV client for Nextcloud file operations."""

import logging
import mimetypes
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape as xml_escape

from httpx import HTTPStatusError, RemoteProtocolError, Response

from nextcloud_mcp_server.observability.metrics import (
    document_download_truncated_total,
    document_scan_truncated_total,
)

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)


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
    """
    content = response.content
    declared = response.headers.get("content-length")
    if declared is None:
        return content
    try:
        expected = int(declared)
    except ValueError:
        # Malformed header — nothing reliable to compare against, so don't
        # raise spuriously; let the (possibly fine) body through.
        return content
    if expected < 0:
        # Degenerate header (negative length) — can't be a real short-read
        # signal and would always trip the check below; ignore it.
        return content
    if len(content) != expected:
        document_download_truncated_total.inc()
        # Log here, not just in the message: both callers funnel this through a
        # generic ``except Exception`` that would otherwise report it as an
        # opaque "Unexpected error reading file".
        logger.warning(
            "Truncated download for %r: expected %d bytes, got %d "
            "(poisoned keep-alive connection? set NEXTCLOUD_HTTP_KEEPALIVE=false "
            "— see #965)",
            label,
            expected,
            len(content),
        )
        raise RemoteProtocolError(
            f"Truncated download for {label!r}: expected {expected} bytes, "
            f"got {len(content)} (poisoned keep-alive connection? see #965)",
            request=response.request,
        )
    return content


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
        """
        return f"{self._get_webdav_base_path()}/{_encode_dav_path(path.lstrip('/'))}"

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
        """Clean up the attachment directory for a note in its old category location."""
        old_category_path_part = f"{old_category}/" if old_category else ""
        old_attachment_dir_path = (
            f"Notes/{old_category_path_part}.attachments.{note_id}/"
        )

        logger.debug(
            "Cleaning up old attachment directory: %s", old_attachment_dir_path
        )
        try:
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

        propfind_body = """<?xml version="1.0"?>
        <d:propfind xmlns:d="DAV:">
            <d:prop>
                <d:displayname/>
                <d:getcontentlength/>
                <d:getcontenttype/>
                <d:getlastmodified/>
                <d:resourcetype/>
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

                items.append(
                    {
                        "name": name,
                        "path": f"{path.rstrip('/')}/{name}" if path else name,
                        "is_directory": is_directory,
                        "size": size if not is_directory else None,
                        "content_type": content_type,
                        "last_modified": modified,
                    }
                )

            logger.debug("Found %s items in directory: %s", len(items), path)
            return items

        except HTTPStatusError as e:
            logger.error("HTTP error listing directory '%s': %s", webdav_path, e)
            raise e
        except Exception as e:
            logger.error("Unexpected error listing directory '%s': %s", webdav_path, e)
            raise e

    async def read_file(self, path: str) -> Tuple[bytes, str]:
        """Read a file's content via WebDAV GET."""
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

            logger.debug("Successfully read file '%s' (%s bytes)", path, len(content))
            return content, content_type

        except HTTPStatusError as e:
            logger.error("HTTP error reading file '%s': %s", path, e)
            raise e
        except Exception as e:
            logger.error("Unexpected error reading file '%s': %s", path, e)
            raise e

    async def write_file(
        self, path: str, content: bytes, content_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """Write content to a file via WebDAV PUT."""
        await self._ensure_principal_id()
        webdav_path = self._webdav_path(path)

        logger.debug("Writing file: %s", path)

        if not content_type:
            content_type, _ = mimetypes.guess_type(path)
            if not content_type:
                content_type = "application/octet-stream"

        headers = {"Content-Type": content_type, "OCS-APIRequest": "true"}

        try:
            response = await self._make_request(
                "PUT", webdav_path, content=content, headers=headers
            )
            response.raise_for_status()

            logger.debug("Successfully wrote file '%s'", path)
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
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

    async def move_resource(
        self, source_path: str, destination_path: str, overwrite: bool = False
    ) -> Dict[str, Any]:
        """Move or rename a resource (file or directory) via WebDAV MOVE.

        Args:
            source_path: The path of the file or directory to move
            destination_path: The new path for the file or directory
            overwrite: Whether to overwrite the destination if it exists

        Returns:
            Dict with status_code and optional message
        """
        await self._ensure_principal_id()
        source_webdav_path = self._webdav_path(source_path)
        destination_webdav_path = self._webdav_path(destination_path)

        # Ensure paths have consistent trailing slashes for directories
        if source_path.endswith("/") and not destination_path.endswith("/"):
            destination_webdav_path += "/"
        elif not source_path.endswith("/") and destination_path.endswith("/"):
            source_webdav_path += "/"

        logger.debug("Moving resource from '%s' to '%s'", source_path, destination_path)

        headers = {
            "OCS-APIRequest": "true",
            "Destination": destination_webdav_path,
            "Overwrite": "T" if overwrite else "F",
        }

        try:
            response = await self._make_request(
                "MOVE", source_webdav_path, headers=headers
            )
            response.raise_for_status()

            logger.debug(
                "Successfully moved resource from '%s' to '%s'",
                source_path,
                destination_path,
            )
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug("Source resource '%s' not found", source_path)
                return {"status_code": 404, "message": "Source resource not found"}
            elif e.response.status_code == 412:
                logger.debug(
                    "Destination '%s' already exists and overwrite is false",
                    destination_path,
                )
                return {
                    "status_code": 412,
                    "message": "Destination already exists and overwrite is false",
                }
            elif e.response.status_code == 409:
                logger.debug(
                    "Parent directory of destination '%s' doesn't exist",
                    destination_path,
                )
                return {
                    "status_code": 409,
                    "message": "Parent directory of destination doesn't exist",
                }
            else:
                logger.error(
                    "HTTP error moving resource from '%s' to '%s': %s",
                    source_path,
                    destination_path,
                    e,
                )
                raise e
        except Exception as e:
            logger.error(
                "Unexpected error moving resource from '%s' to '%s': %s",
                source_path,
                destination_path,
                e,
            )
            raise e

    async def copy_resource(
        self, source_path: str, destination_path: str, overwrite: bool = False
    ) -> Dict[str, Any]:
        """Copy a resource (file or directory) via WebDAV COPY.

        Args:
            source_path: The path of the file or directory to copy
            destination_path: The destination path for the copy
            overwrite: Whether to overwrite the destination if it exists

        Returns:
            Dict with status_code and optional message
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
            "Copying resource from '%s' to '%s'", source_path, destination_path
        )

        headers = {
            "OCS-APIRequest": "true",
            "Destination": destination_webdav_path,
            "Overwrite": "T" if overwrite else "F",
        }

        try:
            response = await self._make_request(
                "COPY", source_webdav_path, headers=headers
            )
            response.raise_for_status()

            logger.debug(
                "Successfully copied resource from '%s' to '%s'",
                source_path,
                destination_path,
            )
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug("Source resource '%s' not found", source_path)
                return {"status_code": 404, "message": "Source resource not found"}
            elif e.response.status_code == 412:
                logger.debug(
                    "Destination '%s' already exists and overwrite is false",
                    destination_path,
                )
                return {
                    "status_code": 412,
                    "message": "Destination already exists and overwrite is false",
                }
            elif e.response.status_code == 409:
                logger.debug(
                    "Parent directory of destination '%s' doesn't exist",
                    destination_path,
                )
                return {
                    "status_code": 409,
                    "message": "Parent directory of destination doesn't exist",
                }
            else:
                logger.error(
                    "HTTP error copying resource from '%s' to '%s': %s",
                    source_path,
                    destination_path,
                    e,
                )
                raise e
        except Exception as e:
            logger.error(
                "Unexpected error copying resource from '%s' to '%s': %s",
                source_path,
                destination_path,
                e,
            )
            raise e

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

        # Build where clause
        where_xml = where_conditions if where_conditions else ""

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
                    item["etag"] = value.strip('"') if value else None
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
        where_conditions = f"""
            <d:like>
                <d:prop>
                    <d:displayname/>
                </d:prop>
                <d:literal>{xml_escape(pattern)}</d:literal>
            </d:like>
        """

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
        where_conditions = f"""
            <d:like>
                <d:prop>
                    <d:getcontenttype/>
                </d:prop>
                <d:literal>{xml_escape(mime_type)}</d:literal>
            </d:like>
        """

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
        where_conditions = f"""
            <d:like>
                <d:prop>
                    <oc:tags/>
                </d:prop>
                <d:literal>%{xml_escape(tag_name)}%</d:literal>
            </d:like>
        """

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
            etag_elem.text.strip('"')
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
            "etag": etag_elem.text.strip('"')
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
