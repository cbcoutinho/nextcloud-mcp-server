import os
from httpx import (
    Client,
    Auth,
    BasicAuth,
    Request,
    Response,
)
import logging
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

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

    def __init__(self, base_url: str, auth: Auth | None = None):

        self._client = Client(
            base_url=base_url,
            auth=auth,
            event_hooks={"request": [log_request], "response": [log_response]},
        )
        HTTPXClientInstrumentor.instrument_client(self._client)

    @classmethod
    def from_env(cls):

        logger.info("Creating NC Client using env vars")

        host = os.environ["NEXTCLOUD_HOST"]
        username = os.environ["NEXTCLOUD_USERNAME"]
        password = os.environ["NEXTCLOUD_PASSWORD"]
        return cls(base_url=host, auth=BasicAuth(username, password))

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
        # body = {"etag": etag} # Removed redundant line
        body = {}
        if title:
            body.update({"title": title})
        if content:
            body.update({"content": content})
        if category:
            body.update({"category": category})

        logger.info(
            "Attempting to update note %s with etag %s, Body: %s, Category: %s",
            note_id,
            etag,  # This was current_etag in the loop
            body,
            category,
        )
        # Ensure conditional PUT using If-Match header is active
        response = self._client.put(
            url=f"/apps/notes/api/v1/notes/{note_id}",
            json=body,
            headers={
                "If-Match": f'"{etag}"'  # NOTE: The `etag` needs to be surrounded by quotes `""`
            },
        )
        logger.info(
            "Update response for note %s: Status %s, Headers %s",
            note_id,
            response.status_code,
            response.headers,
        )
        response.raise_for_status()
        return response.json()

    def notes_search_notes(self, *, query: str):
        all_notes = self.notes_get_all()
        search_results = []
        query_lower = query.lower()
        for note in all_notes:
            title_lower = note.get("title", "").lower()
            content_lower = note.get("content", "").lower()
            if query_lower in title_lower or query_lower in content_lower:
                search_results.append(
                    {
                        "id": note.get("id"),
                        "title": note.get("title"),
                        "category": note.get("category"),
                        "modified": note.get("modified"),
                    }
                )
        return search_results

    def notes_delete_note(self, *, note_id: int):
        response = self._client.delete(f"/apps/notes/api/v1/notes/{note_id}")
        response.raise_for_status()
        return response.json()
