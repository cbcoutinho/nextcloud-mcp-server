"""WebDAV client for Nextcloud file operations."""

import logging
import mimetypes
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from httpx import HTTPStatusError

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)


class WebDAVClient(BaseNextcloudClient):
    """Client for Nextcloud WebDAV operations."""

    app_name = "webdav"

    async def delete_resource(self, path: str) -> Dict[str, Any]:
        """Delete a resource (file or directory) via WebDAV DELETE."""
        # Ensure path ends with a slash if it's a directory
        if not path.endswith("/"):
            path_with_slash = f"{path}/"
        else:
            path_with_slash = path

        webdav_path = f"{self._get_webdav_base_path()}/{path_with_slash.lstrip('/')}"
        logger.debug(f"Deleting WebDAV resource: {webdav_path}")

        headers = {"OCS-APIRequest": "true"}
        try:
            # First try a PROPFIND to verify resource exists
            propfind_headers = {"Depth": "0", "OCS-APIRequest": "true"}
            try:
                propfind_resp = await self._make_request(
                    "PROPFIND", webdav_path, headers=propfind_headers
                )
                logger.debug(
                    f"Resource exists check status: {propfind_resp.status_code}"
                )
            except HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.debug(f"Resource '{path}' doesn't exist, no deletion needed")
                    return {"status_code": 404}
                # For other errors, continue with deletion attempt

            # Proceed with deletion
            response = await self._make_request("DELETE", webdav_path, headers=headers)
            logger.debug(f"Successfully deleted WebDAV resource '{path}'")
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"Resource '{path}' not found, no deletion needed")
                return {"status_code": 404}
            else:
                logger.error(f"HTTP error deleting WebDAV resource '{path}': {e}")
                raise e
        except Exception as e:
            logger.error(f"Unexpected error deleting WebDAV resource '{path}': {e}")
            raise e

    async def cleanup_old_attachment_directory(
        self, note_id: int, old_category: str
    ) -> Dict[str, Any]:
        """Clean up the attachment directory for a note in its old category location."""
        old_category_path_part = f"{old_category}/" if old_category else ""
        old_attachment_dir_path = (
            f"Notes/{old_category_path_part}.attachments.{note_id}/"
        )

        logger.debug(f"Cleaning up old attachment directory: {old_attachment_dir_path}")
        try:
            delete_result = await self.delete_resource(path=old_attachment_dir_path)
            logger.debug(f"Cleanup result: {delete_result}")
            return delete_result
        except Exception as e:
            logger.error(f"Error during cleanup of old attachment directory: {e}")
            raise e

    async def cleanup_note_attachments(
        self, note_id: int, category: str
    ) -> Dict[str, Any]:
        """Clean up attachment directory for a specific note and category."""
        cat_path_part = f"{category}/" if category else ""
        attachment_dir_path = f"Notes/{cat_path_part}.attachments.{note_id}/"

        logger.debug(
            f"Cleaning up attachments for note {note_id} in category '{category}'"
        )
        try:
            delete_result = await self.delete_resource(path=attachment_dir_path)
            logger.debug(f"Cleanup result for note {note_id}: {delete_result}")
            return delete_result
        except Exception as e:
            logger.error(f"Failed cleaning up attachments for note {note_id}: {e}")
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
        # Construct paths based on provided category
        webdav_base = self._get_webdav_base_path()
        category_path_part = f"{category}/" if category else ""
        attachment_dir_segment = f".attachments.{note_id}"
        parent_dir_webdav_rel_path = (
            f"Notes/{category_path_part}{attachment_dir_segment}"
        )
        parent_dir_path = f"{webdav_base}/{parent_dir_webdav_rel_path}"
        attachment_path = f"{parent_dir_path}/{filename}"

        logger.debug(f"Uploading attachment '{filename}' for note {note_id}")

        if not mime_type:
            mime_type, _ = mimetypes.guess_type(filename)
            if not mime_type:
                mime_type = "application/octet-stream"

        headers = {"Content-Type": mime_type, "OCS-APIRequest": "true"}
        try:
            # First check if we can access WebDAV at all
            notes_dir_path = f"{webdav_base}/Notes"
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
                    f"Error accessing WebDAV Notes directory: {notes_dir_response.status_code}"
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
                    f"Unexpected status code {mkcol_response.status_code} when creating attachments directory"
                )
                mkcol_response.raise_for_status()

            # Proceed with the PUT request
            response = await self._make_request(
                "PUT", attachment_path, content=content, headers=headers
            )
            response.raise_for_status()
            logger.debug(
                f"Successfully uploaded attachment '{filename}' to note {note_id}"
            )
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            logger.error(
                f"HTTP error uploading attachment '{filename}' to note {note_id}: {e}"
            )
            raise e
        except Exception as e:
            logger.error(
                f"Unexpected error uploading attachment '{filename}' to note {note_id}: {e}"
            )
            raise e

    async def get_note_attachment(
        self, note_id: int, filename: str, category: Optional[str] = None
    ) -> Tuple[bytes, str]:
        """Fetch a specific attachment from a note via WebDAV GET."""
        webdav_base = self._get_webdav_base_path()
        category_path_part = f"{category}/" if category else ""
        attachment_dir_segment = f".attachments.{note_id}"
        attachment_path = f"{webdav_base}/Notes/{category_path_part}{attachment_dir_segment}/{filename}"

        logger.debug(f"Fetching attachment '{filename}' for note {note_id}")

        try:
            response = await self._make_request("GET", attachment_path)
            response.raise_for_status()

            content = response.content
            mime_type = response.headers.get("content-type", "application/octet-stream")

            logger.debug(
                f"Successfully fetched attachment '{filename}' ({len(content)} bytes)"
            )
            return content, mime_type

        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"Attachment '{filename}' not found for note {note_id}")
            else:
                logger.error(
                    f"HTTP error fetching attachment '{filename}' for note {note_id}: {e}"
                )
            raise e
        except Exception as e:
            logger.error(
                f"Unexpected error fetching attachment '{filename}' for note {note_id}: {e}"
            )
            raise e

    async def list_directory(self, path: str = "") -> List[Dict[str, Any]]:
        """List files and directories in the specified path via WebDAV PROPFIND."""
        webdav_path = f"{self._get_webdav_base_path()}/{path.lstrip('/')}"
        if not webdav_path.endswith("/"):
            webdav_path += "/"

        logger.debug(f"Listing directory: {path}")

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

                # Extract file/directory name from href
                href_text = href.text or ""
                name = href_text.rstrip("/").split("/")[-1]
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

            logger.debug(f"Found {len(items)} items in directory: {path}")
            return items

        except HTTPStatusError as e:
            logger.error(f"HTTP error listing directory '{webdav_path}': {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error listing directory '{webdav_path}': {e}")
            raise e

    async def read_file(self, path: str) -> Tuple[bytes, str]:
        """Read a file's content via WebDAV GET."""
        webdav_path = f"{self._get_webdav_base_path()}/{path.lstrip('/')}"

        logger.debug(f"Reading file: {path}")

        try:
            response = await self._make_request("GET", webdav_path)
            response.raise_for_status()

            content = response.content
            content_type = response.headers.get(
                "content-type", "application/octet-stream"
            )

            logger.debug(f"Successfully read file '{path}' ({len(content)} bytes)")
            return content, content_type

        except HTTPStatusError as e:
            logger.error(f"HTTP error reading file '{path}': {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error reading file '{path}': {e}")
            raise e

    async def write_file(
        self, path: str, content: bytes, content_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """Write content to a file via WebDAV PUT."""
        webdav_path = f"{self._get_webdav_base_path()}/{path.lstrip('/')}"

        logger.debug(f"Writing file: {path}")

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

            logger.debug(f"Successfully wrote file '{path}'")
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            logger.error(f"HTTP error writing file '{path}': {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error writing file '{path}': {e}")
            raise e

    async def create_directory(
        self, path: str, recursive: bool = False
    ) -> Dict[str, Any]:
        """Create a directory via WebDAV MKCOL."""
        webdav_path = f"{self._get_webdav_base_path()}/{path.lstrip('/')}"
        if not webdav_path.endswith("/"):
            webdav_path += "/"

        logger.debug(f"Creating directory: {path}")

        headers = {"OCS-APIRequest": "true"}

        try:
            response = await self._make_request("MKCOL", webdav_path, headers=headers)
            response.raise_for_status()

            logger.debug(f"Successfully created directory '{path}'")
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            # Method Not Allowed - directory already exists
            if e.response.status_code == 405:
                logger.debug(f"Directory '{path}' already exists")
                return {"status_code": 405, "message": "Directory already exists"}

            # File Conflict - parent directory does not exist
            if e.response.status_code == 409 and recursive:
                # Extract parent directory path
                path_parts = path.strip("/").split("/")
                if len(path_parts) > 1:
                    parent_dir = "/".join(path_parts[:-1])
                    logger.debug(
                        f"Parent directory '{parent_dir}' doesn't exist, creating recursively"
                    )
                    await self.create_directory(parent_dir, recursive)
                    # Now try to create the original directory again
                    return await self.create_directory(path, recursive)
                else:
                    # This shouldn't happen for single-level directories under root
                    logger.error(f"409 conflict for single-level directory '{path}'")
                    raise e

            logger.error(f"HTTP error creating directory '{path}': {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error creating directory '{path}': {e}")
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
        source_webdav_path = f"{self._get_webdav_base_path()}/{source_path.lstrip('/')}"
        destination_webdav_path = (
            f"{self._get_webdav_base_path()}/{destination_path.lstrip('/')}"
        )

        # Ensure paths have consistent trailing slashes for directories
        if source_path.endswith("/") and not destination_path.endswith("/"):
            destination_webdav_path += "/"
        elif not source_path.endswith("/") and destination_path.endswith("/"):
            source_webdav_path += "/"

        logger.debug(f"Moving resource from '{source_path}' to '{destination_path}'")

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
                f"Successfully moved resource from '{source_path}' to '{destination_path}'"
            )
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"Source resource '{source_path}' not found")
                return {"status_code": 404, "message": "Source resource not found"}
            elif e.response.status_code == 412:
                logger.debug(
                    f"Destination '{destination_path}' already exists and overwrite is false"
                )
                return {
                    "status_code": 412,
                    "message": "Destination already exists and overwrite is false",
                }
            elif e.response.status_code == 409:
                logger.debug(
                    f"Parent directory of destination '{destination_path}' doesn't exist"
                )
                return {
                    "status_code": 409,
                    "message": "Parent directory of destination doesn't exist",
                }
                logger.debug(
                    f"Parent directory of destination '{destination_path}' doesn't exist"
                )
                return {
                    "status_code": 409,
                    "message": "Parent directory of destination doesn't exist",
                }
            else:
                logger.error(
                    f"HTTP error moving resource from '{source_path}' to '{destination_path}': {e}"
                )
                raise e
        except Exception as e:
            logger.error(
                f"Unexpected error moving resource from '{source_path}' to '{destination_path}': {e}"
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
        source_webdav_path = f"{self._get_webdav_base_path()}/{source_path.lstrip('/')}"
        destination_webdav_path = (
            f"{self._get_webdav_base_path()}/{destination_path.lstrip('/')}"
        )

        # Ensure paths have consistent trailing slashes for directories
        if source_path.endswith("/") and not destination_path.endswith("/"):
            destination_webdav_path += "/"
        elif not source_path.endswith("/") and destination_path.endswith("/"):
            source_webdav_path += "/"

        logger.debug(f"Copying resource from '{source_path}' to '{destination_path}'")

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
                f"Successfully copied resource from '{source_path}' to '{destination_path}'"
            )
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"Source resource '{source_path}' not found")
                return {"status_code": 404, "message": "Source resource not found"}
            elif e.response.status_code == 412:
                logger.debug(
                    f"Destination '{destination_path}' already exists and overwrite is false"
                )
                return {
                    "status_code": 412,
                    "message": "Destination already exists and overwrite is false",
                }
            elif e.response.status_code == 409:
                logger.debug(
                    f"Parent directory of destination '{destination_path}' doesn't exist"
                )
                return {
                    "status_code": 409,
                    "message": "Parent directory of destination doesn't exist",
                }
            else:
                logger.error(
                    f"HTTP error copying resource from '{source_path}' to '{destination_path}': {e}"
                )
                raise e
        except Exception as e:
            logger.error(
                f"Unexpected error copying resource from '{source_path}' to '{destination_path}': {e}"
            )
            raise e

    async def search_files(
        self,
        scope: str = "",
        where_conditions: Optional[str] = None,
        properties: Optional[List[str]] = None,
        order_by: Optional[List[Tuple[str, str]]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Search for files using WebDAV SEARCH method (RFC 5323).

        Args:
            scope: Directory path to search in (empty string for user root)
            where_conditions: XML string for where clause conditions
            properties: List of property names to retrieve (defaults to basic set)
            order_by: List of (property, direction) tuples for sorting, e.g. [("getlastmodified", "descending")]
            limit: Maximum number of results to return

        Returns:
            List of file/directory dictionaries with requested properties
        """
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
        )

        # The SEARCH endpoint is at the dav root
        search_path = "/remote.php/dav/"

        headers = {"Content-Type": "text/xml", "OCS-APIRequest": "true"}

        logger.debug(f"Searching files in scope: {scope}")

        try:
            response = await self._make_request(
                "SEARCH", search_path, content=search_body, headers=headers
            )
            response.raise_for_status()

            # Parse the XML response
            results = self._parse_search_response(response.content, scope)

            logger.debug(f"Search returned {len(results)} results")
            return results

        except HTTPStatusError as e:
            logger.error(f"HTTP error during search: {e}")
            raise e
        except Exception as e:
            logger.error(f"Unexpected error during search: {e}")
            raise e

    def _build_search_xml(
        self,
        scope: str,
        where_conditions: Optional[str],
        properties: List[str],
        order_by: Optional[List[Tuple[str, str]]],
        limit: Optional[int],
    ) -> str:
        """Build the XML body for a SEARCH request."""
        # Construct the scope path
        username = self.username
        scope_path = f"/files/{username}"
        if scope:
            scope_path = f"{scope_path}/{scope.lstrip('/')}"

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

        # Build limit clause
        limit_xml = (
            f"<d:limit><d:nresults>{limit}</d:nresults></d:limit>" if limit else ""
        )

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
                <d:href>{scope_path}</d:href>
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

            # Extract file/directory path from href
            href_text = href.text or ""
            # Remove the /remote.php/dav/files/username/ prefix to get relative path
            path_parts = href_text.split("/files/")
            if len(path_parts) > 1:
                # Get the path after username
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
                <d:literal>{pattern}</d:literal>
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
        """
        where_conditions = f"""
            <d:like>
                <d:prop>
                    <d:getcontenttype/>
                </d:prop>
                <d:literal>{mime_type}</d:literal>
            </d:like>
        """

        return await self.search_files(
            scope=scope, where_conditions=where_conditions, limit=limit
        )

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
                <d:literal>%{tag_name}%</d:literal>
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

        logger.debug(f"Retrieved file info for ID {file_id}: {name}")
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

        response = await self._client.request(
            "PROPFIND",
            "/remote.php/dav/systemtags/",
            headers={"Depth": "1"},
            content=propfind_body,
        )
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
                logger.debug(f"Found tag '{tag_name}' with ID {tag_info['id']}")
                return tag_info

        logger.debug(f"Tag '{tag_name}' not found")
        return None

    async def get_files_by_tag(self, tag_id: int) -> list[dict[str, Any]]:
        """Get all files tagged with a specific system tag via WebDAV REPORT.

        Args:
            tag_id: Numeric ID of the tag

        Returns:
            List of file info dictionaries with path, size, content_type, etc.
        """
        # Use WebDAV REPORT method with systemtag filter, requesting all properties
        report_body = f"""<?xml version="1.0"?>
<oc:filter-files xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <oc:fileid/>
    <d:displayname/>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <d:getlastmodified/>
    <d:getetag/>
  </d:prop>
  <oc:filter-rules>
    <oc:systemtag>{tag_id}</oc:systemtag>
  </oc:filter-rules>
</oc:filter-files>"""

        response = await self._client.request(
            "REPORT",
            f"{self._get_webdav_base_path()}/",
            content=report_body,
        )
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

            if fileid_elem is None or not fileid_elem.text:
                continue

            # Decode href path and extract the file path
            from urllib.parse import unquote

            href_path = unquote(href_elem.text)
            # Remove WebDAV prefix to get user-relative path
            webdav_prefix = f"/remote.php/dav/files/{self.username}/"
            file_path = href_path.replace(webdav_prefix, "/")

            # Parse last modified timestamp
            last_modified_timestamp = None
            if lastmodified_elem is not None and lastmodified_elem.text:
                from email.utils import parsedate_to_datetime

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
            }
            files.append(file_info)

        logger.debug(f"Found {len(files)} files with tag ID {tag_id}")
        return files

    async def get_file_info(self, path: str) -> dict[str, Any] | None:
        """Get file info including file ID via WebDAV PROPFIND.

        Args:
            path: Path to the file (relative to user's files directory)

        Returns:
            File info dictionary with id, name, size, content_type, etc.
            Returns None if file not found.
        """
        webdav_path = f"{self._get_webdav_base_path()}/{path.lstrip('/')}"

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

        try:
            response = await self._client.request(
                "PROPFIND",
                webdav_path,
                headers={"Depth": "0"},
                content=propfind_body,
            )
            response.raise_for_status()
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"File not found: {path}")
                return None
            raise

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

        logger.debug(f"Got file info for '{path}': id={file_info['id']}")
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

        logger.info(f"Created tag '{name}' with ID {tag_info['id']}")
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
            logger.debug(f"Tag '{name}' already exists with ID {existing_tag['id']}")
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
            logger.info(f"Tagged file {file_id} with tag {tag_id}")
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
            logger.info(f"Removed tag {tag_id} from file {file_id}")
            return True

        response.raise_for_status()
        return True
