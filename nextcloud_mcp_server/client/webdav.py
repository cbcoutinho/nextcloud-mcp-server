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
