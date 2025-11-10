"""Client for Nextcloud Notes app operations."""

import logging
from typing import Any, AsyncIterator, Dict, Optional

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)


class NotesClient(BaseNextcloudClient):
    """Client for Nextcloud Notes app operations."""

    app_name = "notes"

    async def get_settings(self) -> Dict[str, Any]:
        """Get Notes app settings."""
        response = await self._make_request("GET", "/apps/notes/api/v1/settings")
        return response.json()

    async def get_all_notes(
        self, prune_before: Optional[int] = None
    ) -> AsyncIterator[Dict[str, Any]]:
        """Get all notes, yielding them one at a time.

        The Notes API returns changed notes with full data in chunks, and ALL note IDs
        (with only 'id' field) in the last chunk for deletion detection. This causes
        duplicates which we handle by tracking seen IDs (first occurrence with full
        data is kept, later pruned duplicates are skipped).

        Args:
            prune_before: Optional Unix timestamp. Notes unchanged since this time
                         are pruned (only 'id' field returned in last chunk).
                         Reduces data transfer for large note collections.

        Yields:
            Note dictionaries with full data (deduplicated).
        """
        cursor = ""
        seen_ids: set[int] = set()

        while True:
            params: Dict[str, Any] = {"chunkSize": 10}
            if cursor:
                params["chunkCursor"] = cursor
            if prune_before is not None:
                params["pruneBefore"] = prune_before

            response = await self._make_request(
                "GET",
                "/apps/notes/api/v1/notes",
                params=params,
            )
            response_data = response.json()

            for note in response_data:
                note_id = note.get("id")
                if note_id is None:
                    logger.warning(f"Skipping note without ID: {note}")
                    continue

                # Skip duplicates (API returns all IDs in last chunk for deletion detection)
                if note_id in seen_ids:
                    logger.debug(
                        f"Skipping duplicate note {note_id} (pruned version in last chunk)"
                    )
                    continue

                seen_ids.add(note_id)
                yield note

            if "X-Notes-Chunk-Cursor" not in response.headers:
                break
            cursor = response.headers["X-Notes-Chunk-Cursor"]

    async def get_note(self, note_id: int) -> Dict[str, Any]:
        """Get a specific note by ID."""
        response = await self._make_request(
            "GET", f"/apps/notes/api/v1/notes/{note_id}"
        )
        return response.json()

    async def create_note(
        self,
        title: Optional[str] = None,
        content: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new note."""
        body = {}
        if title:
            body["title"] = title
        if content:
            body["content"] = content
        if category:
            body["category"] = category

        response = await self._make_request(
            "POST", "/apps/notes/api/v1/notes", json=body
        )
        return response.json()

    async def update(
        self,
        note_id: int,
        etag: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an existing note."""
        # Get current note details to check for category change
        old_note = None
        try:
            if category is not None:
                old_note = await self.get_note(note_id)
                old_category = old_note.get("category", "")
                logger.info(f"Current category for note {note_id}: '{old_category}'")
        except Exception as e:
            logger.warning(
                f"Could not fetch current note {note_id} details before update: {e}"
            )
            old_note = None

        # Prepare update body
        body = {}
        if title:
            body["title"] = title
        if content:
            body["content"] = content
        if category:
            body["category"] = category

        logger.info(
            f"Attempting to update note {note_id} with etag {etag}. Body: {body}"
        )

        response = await self._make_request(
            "PUT",
            f"/apps/notes/api/v1/notes/{note_id}",
            json=body,
            headers={"If-Match": f'"{etag}"'},
        )

        logger.info(
            f"Update response for note {note_id}: Status {response.status_code}"
        )
        updated_note = response.json()

        # Check for category change and cleanup old attachment directory if needed
        if (
            old_note
            and category is not None
            and old_note.get("category", "") != category
        ):
            logger.info(
                f"Category changed from '{old_note.get('category', '')}' to '{category}' - cleaning up old attachment directory"
            )
            try:
                # Import here to avoid circular imports
                from .webdav import WebDAVClient

                webdav_client = WebDAVClient(self._client, self.username)
                await webdav_client.cleanup_old_attachment_directory(
                    note_id=note_id, old_category=old_note.get("category", "")
                )
            except Exception as e:
                logger.error(
                    f"Error cleaning up old attachment directory for note {note_id}: {e}"
                )

        return updated_note

    async def delete_note(self, note_id: int) -> Dict[str, Any]:
        """Delete a note and its attachments."""
        # Fetch note details first to get category for cleanup
        try:
            note_details = await self.get_note(note_id)
            category = note_details.get("category", "")

            # Determine potential categories for cleanup
            potential_categories = []
            if category:
                potential_categories.append(category)
            if category != "":
                potential_categories.append("")  # Empty category

            logger.info(
                f"Note {note_id} has category: '{category}', will check attachment directories in: {potential_categories}"
            )
        except Exception as e:
            logger.warning(
                f"Could not fetch note {note_id} details before deletion: {e}"
            )
            potential_categories = ["", "Unknown"]  # Try common categories

        # Delete the note via API
        logger.info(f"Deleting note {note_id} via API")
        response = await self._make_request(
            "DELETE", f"/apps/notes/api/v1/notes/{note_id}"
        )
        logger.info(f"Note {note_id} deleted successfully via API")
        json_response = response.json()

        # Clean up attachment directories
        try:
            from .webdav import WebDAVClient

            webdav_client = WebDAVClient(self._client, self.username)

            for cat in potential_categories:
                try:
                    await webdav_client.cleanup_note_attachments(note_id, cat)
                except Exception as e:
                    logger.warning(
                        f"Failed to cleanup attachments for category '{cat}': {e}"
                    )
        except Exception as e:
            logger.warning(f"Error during attachment cleanup: {e}")

        return json_response

    async def append_content(self, note_id: int, content: str) -> Dict[str, Any]:
        """Append content to an existing note with a separator."""
        logger.info(f"Appending content to note {note_id}")

        # Get current note
        current_note = await self.get_note(note_id)

        # Use fixed separator for consistency
        separator = "\n---\n"

        # Combine content
        existing_content = current_note.get("content", "")
        if existing_content:
            new_content = existing_content + separator + content
        else:
            new_content = content  # No separator needed for empty notes

        logger.info(
            f"Combining existing content ({len(existing_content)} chars) with new content ({len(content)} chars)"
        )

        # Update with combined content
        return await self.update(
            note_id=note_id,
            etag=current_note["etag"],
            content=new_content,
            title=None,  # Keep existing title
            category=None,  # Keep existing category
        )
