import os
import time  # Import time for sleep
import mimetypes
from io import BytesIO
from httpx import (
    Client,
    Auth,
    BasicAuth,
    Headers,
    Request,
    Response,
    HTTPStatusError,
)  # Import HTTPStatusError
import logging


logger = logging.getLogger(__name__)


def log_request(request: Request):
    logger.info(
        "Request event hook ****: %s %s - Waiting for content",
        request.method,
        request.url,
    )
    logger.info("Request body: %s", request.content)
    logger.info("Headers: %s", request.headers)


def log_response(response: Response):
    response.read()  # Explicitly read the stream before accessing .text
    logger.info("Response [%s] %s", response.status_code, response.text)


class NextcloudClient:

    def __init__(self, base_url: str, username: str, auth: Auth | None = None):
        self.username = username # Store username
        self._client = Client(
            base_url=base_url,
            auth=auth,
            event_hooks={"request": [log_request], "response": [log_response]},
        )

    @classmethod
    def from_env(cls):

        logger.info("Creating NC Client using env vars")

        host = os.environ["NEXTCLOUD_HOST"]
        username = os.environ["NEXTCLOUD_USERNAME"]
        password = os.environ["NEXTCLOUD_PASSWORD"]
        # Pass username to constructor
        return cls(base_url=host, username=username, auth=BasicAuth(username, password))

    def capabilities(self):

        response = self._client.get(
            "/ocs/v2.php/cloud/capabilities",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()

        return response.json()

    def notes_get_settings(self):
        response = self._client.get("/apps/notes/api/v1/settings")
        response.raise_for_status()
        return response.json()

    def notes_get_all(self):
        response = self._client.get("/apps/notes/api/v1/notes")
        response.raise_for_status()
        return response.json()

    def notes_get_note(self, *, note_id: int):
        response = self._client.get(f"/apps/notes/api/v1/notes/{note_id}")
        response.raise_for_status()
        return response.json()

    def notes_create_note(
        self,
        *,
        title: str | None = None,
        content: str | None = None,
        category: str | None = None,
    ):
        body = {}
        if title:
            body.update({"title": title})
        if content:
            body.update({"content": content})
        if category:
            body.update({"category": category})

        response = self._client.post(
            url="/apps/notes/api/v1/notes",
            json=body,
        )
        response.raise_for_status()
        return response.json()

    def notes_update_note(
        self,
        *,
        note_id: int,
        etag: str,
        title: str | None = None,
        content: str | None = None,
        category: str | None = None,
    ):
        # First, get the current note details to check for category change
        old_note = None
        try:
            if category is not None:  # Only fetch if category might change
                old_note = self.notes_get_note(note_id=note_id)
                old_category = old_note.get("category", "")
                logger.info(f"Current category for note {note_id}: '{old_category}'")
        except Exception as e:
            logger.warning(f"Could not fetch current note {note_id} details before update: {e}")
            # Continue with update even if we couldn't fetch current details
            old_note = None

        # Prepare update body
        body = {}
        if title:
            body.update({"title": title})
        if content:
            body.update({"content": content})
        if category:
            body.update({"category": category})

        logger.info(
            "Attempting to update note %s with etag %s. Body: %s",
            note_id,
            etag,
            body,
        )
        # Ensure conditional PUT using If-Match header is active
        response = self._client.put(
            url=f"/apps/notes/api/v1/notes/{note_id}",
            json=body,
            headers={"If-Match": f'"{etag}"'},
        )
        logger.info(
            "Update response for note %s: Status %s, Headers %s",
            note_id,
            response.status_code,
            response.headers,
        )
        response.raise_for_status()
        updated_note = response.json()

        # Check for category change and clean up old attachment directory if needed
        if old_note and category is not None and old_note.get("category", "") != category:
            logger.info(f"Category changed from '{old_note.get('category', '')}' to '{category}' - cleaning up old attachment directory")
            try:
                self._cleanup_old_attachment_directory(note_id=note_id, old_category=old_note.get("category", ""))
            except Exception as e:
                logger.error(f"Error cleaning up old attachment directory for note {note_id}: {e}")
                # Continue with update even if cleanup failed
        
        return updated_note

    def notes_search_notes(self, *, query: str):
        """
        Search notes using token-based matching with relevance ranking.
        Returns notes sorted by relevance score.
        """
        all_notes = self.notes_get_all()
        search_results = []

        # Process the query
        query_tokens = self.process_query(query)

        # If empty query after processing, return empty results
        if not query_tokens:
            return []

        # Process and score each note
        for note in all_notes:
            title_tokens, content_tokens = self.process_note_content(note)
            score = self.calculate_score(query_tokens, title_tokens, content_tokens)

            # Only include notes with a non-zero score
            if score > 0:
                search_results.append({
                    "id": note.get("id"),
                    "title": note.get("title"),
                    "category": note.get("category"),
                    "modified": note.get("modified"),
                    "_score": score  # Include score for sorting (optional field)
                })

        # Sort by score in descending order
        search_results.sort(key=lambda x: x["_score"], reverse=True)

        # Keep score field for debugging
        # for result in search_results:
        #     if "_score" in result:
        #         del result["_score"]

        return search_results

    def process_query(self, query: str) -> list[str]:
        """
        Tokenize and normalize the search query.
        """
        # Convert to lowercase and split into tokens
        tokens = query.lower().split()
        # Filter out very short tokens (optional)
        tokens = [token for token in tokens if len(token) > 1]
        # Could add stop word removal here
        return tokens

    def process_note_content(self, note: dict) -> tuple[list[str], list[str]]:
        """
        Tokenize and normalize note title and content.
        """
        # Process title
        title = note.get("title", "").lower()
        title_tokens = title.split()

        # Process content
        content = note.get("content", "").lower()
        content_tokens = content.split()

        return title_tokens, content_tokens

    def calculate_score(self, query_tokens: list[str], title_tokens: list[str], content_tokens: list[str]) -> float:
        """
        Calculate a relevance score for a note based on query tokens.
        """
        # Constants for weighting
        TITLE_WEIGHT = 3.0
        CONTENT_WEIGHT = 1.0

        score = 0.0

        # Count matches in title
        title_matches = sum(1 for qt in query_tokens if qt in title_tokens)
        if query_tokens:  # Avoid division by zero
            title_match_ratio = title_matches / len(query_tokens)
            score += TITLE_WEIGHT * title_match_ratio

        # Count matches in content
        content_matches = sum(1 for qt in query_tokens if qt in content_tokens)
        if query_tokens:  # Avoid division by zero
            content_match_ratio = content_matches / len(query_tokens)
            score += CONTENT_WEIGHT * content_match_ratio

        # If no tokens matched at all, return zero
        if title_matches == 0 and content_matches == 0:
            return 0.0

        return score

    def _cleanup_old_attachment_directory(self, *, note_id: int, old_category: str):
        """
        Clean up the attachment directory for a note in its old category location.
        Called after a category change to prevent orphaned directories.
        """
        # Construct path to old attachment directory
        old_category_path_part = f"{old_category}/" if old_category else ""
        old_attachment_dir_path = f"Notes/{old_category_path_part}.attachments.{note_id}/"
        
        logger.info(f"Cleaning up old attachment directory: {old_attachment_dir_path}")
        try:
            delete_result = self.delete_webdav_resource(path=old_attachment_dir_path)
            logger.info(f"Cleanup of old attachment directory result: {delete_result}")
            return delete_result
        except Exception as e:
            logger.error(f"Error during cleanup of old attachment directory: {e}")
            raise e

    def delete_webdav_resource(self, *, path: str):
        """Delete a resource (file or directory) via WebDAV DELETE."""
        # Ensure path ends with a slash if it's a directory
        if not path.endswith('/'):
             # This is a heuristic; a more robust solution would check resource type first
             # but for the specific case of deleting the attachment directory, this is acceptable.
             path_with_slash = f"{path}/"
        else:
             path_with_slash = path

        webdav_path = f"{self._get_webdav_base_path()}/{path_with_slash.lstrip('/')}"
        logger.info("Deleting WebDAV resource: %s", webdav_path)

        headers = {"OCS-APIRequest": "true"}
        try:
            # First try a PROPFIND to verify resource exists
            propfind_headers = {"Depth": "0", "OCS-APIRequest": "true"}
            try:
                propfind_resp = self._client.request("PROPFIND", webdav_path, headers=propfind_headers)
                logger.info(f"Resource exists check (PROPFIND) status: {propfind_resp.status_code}")
                # If we get here with 2xx, the resource exists
            except HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.info(f"Resource '{webdav_path}' doesn't exist, no deletion needed.")
                    return {"status_code": 404}
                # For other errors, continue with deletion attempt
            
            # Proceed with deletion
            response = self._client.delete(webdav_path, headers=headers)
            response.raise_for_status() # Raises for 4xx/5xx status codes
            logger.info("Successfully deleted WebDAV resource '%s' (Status: %s)", webdav_path, response.status_code)
            # DELETE typically returns 204 No Content on success
            return {"status_code": response.status_code}

        except HTTPStatusError as e:
            logger.error(
                "HTTP error deleting WebDAV resource '%s': %s",
                webdav_path,
                e,
            )
            # It's expected to get a 404 if the resource doesn't exist, which is fine.
            # We only re-raise if it's not a 404.
            if e.response.status_code != 404:
                raise e
            else:
                logger.info("Resource '%s' not found, no deletion needed.", webdav_path)
                return {"status_code": 404} # Indicate resource was not found
        except Exception as e:
            logger.error(
                "Unexpected error deleting WebDAV resource '%s': %s",
                webdav_path,
                e,
            )
            raise e

    def notes_delete_note(self, *, note_id: int):
        """Deletes a note via API and attempts to delete its attachment directory via WebDAV."""
        # Fetch note details first to get the category for path construction
        try:
            note_details = self.notes_get_note(note_id=note_id)
            category = note_details.get("category", "")
            
            # Check for other potential categories (if any note was moved between categories)
            # We can't reliably detect this without a dedicated tracking mechanism, but we can
            # implement a basic check for common category names and empty category
            potential_categories = []
            if category:
                potential_categories.append(category)  # Current category first
            
            # Add empty category (uncategorized notes)
            if category != "":
                potential_categories.append("")
                
            # We could add logic here to check for other common categories if needed
            
            logger.info(f"Note {note_id} has category: '{category}', will check attachment directories in: {potential_categories}")
        except HTTPStatusError as e:
            # If note doesn't exist (404), we can't delete attachments anyway.
            # Re-raise other errors.
            if e.response.status_code == 404:
                logger.warning(f"Note {note_id} not found when attempting delete. Skipping attachment cleanup.")
                # Still raise the 404 as the primary delete operation failed
                raise e
            else:
                logger.error(f"Error fetching note {note_id} details before deleting attachments: {e}")
                raise e # Re-raise unexpected errors during fetch

        # Proceed with API note deletion
        logger.info(f"Deleting note {note_id} via API.")
        response = self._client.delete(f"/apps/notes/api/v1/notes/{note_id}")
        response.raise_for_status() # Raise if API deletion fails
        logger.info(f"Note {note_id} deleted successfully via API.")
        json_response = response.json() # Usually empty on success

        # Now, attempt to delete the associated attachments directory via WebDAV for each potential category
        for cat in potential_categories:
            cat_path_part = f"{cat}/" if cat else ""
            attachment_dir_path = f"Notes/{cat_path_part}.attachments.{note_id}/"

            logger.info(f"Attempting to delete attachment directory for note {note_id} in category '{cat}' via WebDAV: {attachment_dir_path}")
            try:
                # delete_webdav_resource expects path relative to user's files dir
                delete_result = self.delete_webdav_resource(path=attachment_dir_path)
                logger.info(f"WebDAV deletion for category '{cat}' attachment directory: {delete_result}")
            except Exception as e:
                # Log the error but don't re-raise, as API note deletion itself was successful
                # Also, we want to try other potential categories even if one fails
                logger.error(f"Failed during WebDAV deletion for category '{cat}' attachment directory: {e}")

        return json_response

    # Removed incorrect get_note_attachment method that used Notes API

    def _get_webdav_base_path(self) -> str:
        """Helper to get the base WebDAV path for the authenticated user."""
        # Use the stored username
        return f"/remote.php/dav/files/{self.username}"

    # Removed _get_note_attachment_webdav_path helper

    def add_note_attachment(self, *, note_id: int, filename: str, content: bytes, category: str | None = None, mime_type: str | None = None):
        """
        Add/Update an attachment to a note via WebDAV PUT.
        Requires the caller to provide the note's category.
        """
        # Construct paths based on provided category
        webdav_base = self._get_webdav_base_path()
        category_path_part = f"{category}/" if category else ""
        attachment_dir_segment = f".attachments.{note_id}"
        parent_dir_webdav_rel_path = f"Notes/{category_path_part}{attachment_dir_segment}"
        parent_dir_path = f"{webdav_base}/{parent_dir_webdav_rel_path}" # Full path for MKCOL
        attachment_path = f"{parent_dir_path}/{filename}" # Full path for PUT

        logger.info(f"Uploading attachment for note {note_id} (category: '{category or ''}') to WebDAV path: {attachment_path}")

        # Log current auth settings to diagnose the issue
        logger.info("WebDAV auth settings - Username: %s, Auth Type: %s", 
                   self.username, type(self._client.auth).__name__)

        if not mime_type:
            mime_type, _ = mimetypes.guess_type(filename)
            if not mime_type:
                mime_type = "application/octet-stream" # Default if guessing fails

        headers = {"Content-Type": mime_type, "OCS-APIRequest": "true"}
        try:
            # First check if we can access WebDAV at all with current credentials
            # by checking the Notes directory
            notes_dir_path = f"{webdav_base}/Notes"
            logger.info("Testing WebDAV access to Notes directory: %s", notes_dir_path)
            
            # Log details of the auth being used by the client for this specific request
            if self._client.auth:
                auth_header = self._client.auth.auth_flow(self._client.build_request("GET", notes_dir_path)).__next__().headers.get("Authorization")
                logger.info("Authorization header for PROPFIND (Notes dir): %s", auth_header if auth_header else "Not present or generated by auth flow")
            else:
                logger.info("No httpx.Auth object configured on the client for PROPFIND (Notes dir).")

            propfind_headers = {"Depth": "0", "OCS-APIRequest": "true"}
            logger.info("Headers for PROPFIND (Notes dir): %s", propfind_headers)
            notes_dir_response = self._client.request("PROPFIND", notes_dir_path, 
                                                    headers=propfind_headers)
            
            if notes_dir_response.status_code == 401:
                logger.error("WebDAV authentication failed for Notes directory. Please verify WebDAV permissions.")
                raise HTTPStatusError(
                    f"Authentication error accessing WebDAV Notes directory: {notes_dir_response.status_code}",
                    request=notes_dir_response.request,
                    response=notes_dir_response
                )
            elif notes_dir_response.status_code >= 400:
                logger.error("Error accessing WebDAV Notes directory: %s", notes_dir_response.status_code)
                notes_dir_response.raise_for_status()
            else:
                logger.info("Successfully accessed WebDAV Notes directory (Status: %s)", 
                           notes_dir_response.status_code)
            
            # Ensure the parent directory exists using MKCOL
            # parent_dir_path is now determined by the helper method
            logger.info("Ensuring attachments directory exists: %s", parent_dir_path)
            mkcol_headers = {"OCS-APIRequest": "true"}
            logger.info("Headers for MKCOL (Attachments dir): %s", mkcol_headers)
            mkcol_response = self._client.request("MKCOL", parent_dir_path, headers=mkcol_headers)
            # MKCOL should return 201 Created or 405 Method Not Allowed (if directory already exists)
            # We can ignore 405, but raise for other errors
            if mkcol_response.status_code not in [201, 405]:
                logger.warning(
                    "Unexpected status code %s when creating attachments directory",
                    mkcol_response.status_code
                )
                mkcol_response.raise_for_status()
            else:
                logger.info("Created/verified directory: %s (Status: %s)", 
                           parent_dir_path, mkcol_response.status_code)

            # Proceed with the PUT request
            logger.info("Putting attachment file to: %s", attachment_path)
            response = self._client.put(
                attachment_path,
                content=content,
                headers=headers
            )
            response.raise_for_status() # Raises for 4xx/5xx status codes
            logger.info("Successfully uploaded attachment '%s' to note %s (Status: %s)", filename, note_id, response.status_code)
            # PUT typically returns 201 Created or 204 No Content on success
            return {"status_code": response.status_code} # Return status or relevant info

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

    def get_note_attachment(self, *, note_id: int, filename: str, category: str | None = None):
        """
        Fetch a specific attachment from a note via WebDAV GET.
        Requires the caller to provide the note's category.
        """
        # Construct path based on provided category
        webdav_base = self._get_webdav_base_path()
        category_path_part = f"{category}/" if category else ""
        attachment_dir_segment = f".attachments.{note_id}"
        attachment_path = f"{webdav_base}/Notes/{category_path_part}{attachment_dir_segment}/{filename}"

        logger.info(f"Fetching attachment for note {note_id} (category: '{category or ''}') from WebDAV path: {attachment_path}")

        try:
            response = self._client.get(attachment_path)
            response.raise_for_status()

            content = response.content
            mime_type = response.headers.get("content-type", "application/octet-stream")

            logger.info("Successfully fetched attachment '%s' (%s, %d bytes)", filename, mime_type, len(content))
            return content, mime_type

        except HTTPStatusError as e:
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
