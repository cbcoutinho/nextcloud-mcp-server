"""Client for Nextcloud Notes app operations."""

import logging
from typing import Any, AsyncIterator, Dict, Optional

from .base import BaseNextcloudClient
from .webdav import WebDAVClient

logger = logging.getLogger(__name__)


def _expect_note_object(payload: Any, *, operation: str) -> Dict[str, Any]:
    """Coerce a Notes API single-note response into a dict.

    Notes v5.0.0 has a catch-all route (``notes_api#fail``) that returns ``[]``
    as JSON for unmatched paths, and a few edge cases where the response is a
    list-wrapped object instead of a bare object — see issue #730. Without this
    guard, callers hit a cryptic Pydantic ``"argument after ** must be a mapping,
    not list"`` from ``Note(**payload)``.

    Returns the dict unchanged. If the payload is a single-element list, returns
    the inner dict. Anything else (empty list, list of multiple, non-dict) raises
    a clear ``ValueError`` so the failure mode is obvious in logs.
    """
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        if len(payload) == 1 and isinstance(payload[0], dict):
            logger.warning(
                "Notes API returned a single-element list for %s; unwrapping. "
                "This is a Notes app v5.0.0 quirk — see #730.",
                operation,
            )
            return payload[0]
        raise ValueError(
            f"{operation}: Notes API returned a list-shaped payload "
            f"({len(payload)} elements) where a single note object was expected. "
            f"This typically means the request was routed to the catch-all "
            f"notes_api#fail handler (e.g. unmatched URL or wrong API version). "
            f"Verify the Notes app version and URL prefix (#732)."
        )
    raise ValueError(
        f"{operation}: Notes API returned an unexpected payload type "
        f"({type(payload).__name__}) where a single note object was expected."
    )


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
            params: Dict[str, Any] = {"chunkSize": 100}
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
                    logger.warning("Skipping note without ID: %s", note)
                    continue

                # Skip duplicates (API returns all IDs in last chunk for deletion detection)
                if note_id in seen_ids:
                    logger.debug(
                        "Skipping duplicate note %s (pruned version in last chunk)",
                        note_id,
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
        return _expect_note_object(response.json(), operation="get_note")

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
        return _expect_note_object(response.json(), operation="create_note")

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
                logger.info("Current category for note %s: '%s'", note_id, old_category)
        except Exception as e:
            logger.warning(
                "Could not fetch current note %s details before update: %s", note_id, e
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
            "Attempting to update note %s with etag %s. Body: %s", note_id, etag, body
        )

        response = await self._make_request(
            "PUT",
            f"/apps/notes/api/v1/notes/{note_id}",
            json=body,
            headers={"If-Match": f'"{etag}"'},
        )

        logger.info(
            "Update response for note %s: Status %s", note_id, response.status_code
        )
        updated_note = _expect_note_object(response.json(), operation="update_note")

        # Check for category change and cleanup old attachment directory if needed
        if (
            old_note
            and category is not None
            and old_note.get("category", "") != category
        ):
            logger.info(
                "Category changed from '%s' to '%s' - cleaning up old attachment directory",
                old_note.get("category", ""),
                category,
            )
            try:
                webdav_client = WebDAVClient(self._client, self.username)
                await webdav_client.cleanup_old_attachment_directory(
                    note_id=note_id, old_category=old_note.get("category", "")
                )
            except Exception as e:
                logger.error(
                    "Error cleaning up old attachment directory for note %s: %s",
                    note_id,
                    e,
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
                "Note %s has category: '%s', will check attachment directories in: %s",
                note_id,
                category,
                potential_categories,
            )
        except Exception as e:
            logger.warning(
                "Could not fetch note %s details before deletion: %s", note_id, e
            )
            potential_categories = ["", "Unknown"]  # Try common categories

        # Delete the note via API
        logger.info("Deleting note %s via API", note_id)
        response = await self._make_request(
            "DELETE", f"/apps/notes/api/v1/notes/{note_id}"
        )
        logger.info("Note %s deleted successfully via API", note_id)
        json_response = response.json()

        # Clean up attachment directories
        try:
            webdav_client = WebDAVClient(self._client, self.username)

            for cat in potential_categories:
                try:
                    await webdav_client.cleanup_note_attachments(note_id, cat)
                except Exception as e:
                    logger.warning(
                        "Failed to cleanup attachments for category '%s': %s", cat, e
                    )
        except Exception as e:
            logger.warning("Error during attachment cleanup: %s", e)

        return json_response

    async def append_content(self, note_id: int, content: str) -> Dict[str, Any]:
        """Append content to an existing note with a separator."""
        logger.info("Appending content to note %s", note_id)

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
            "Combining existing content (%s chars) with new content (%s chars)",
            len(existing_content),
            len(content),
        )

        # Update with combined content
        return await self.update(
            note_id=note_id,
            etag=current_note["etag"],
            content=new_content,
            title=None,  # Keep existing title
            category=None,  # Keep existing category
        )
