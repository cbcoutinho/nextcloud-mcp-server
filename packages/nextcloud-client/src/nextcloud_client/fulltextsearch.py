"""Client for the Nextcloud Full Text Search app (``fulltextsearch``).

Wraps ``GET /apps/fulltextsearch/v1/remote``, which takes the whole search request
as one JSON-encoded ``request`` parameter. It is the app's ``@NoCSRFRequired`` route
and, unlike the unified search provider, reports the total number of hits.

The server needs ``fulltextsearch``, a search platform such as
``fulltextsearch_elasticsearch`` and the ``files_fulltextsearch`` provider.
"""

import json
from typing import Any, Literal

from .base import BaseNextcloudClient

FullTextSearchMatch = Literal["all", "content", "filename"]


class FullTextSearchError(Exception):
    """The app answered, but reported that the search failed."""


def _case_insensitive(extension: str) -> str:
    """Spell an extension so that it matches in either case.

    ``files_fulltextsearch`` puts the value into a case-sensitive regular
    expression, so "pdf" alone would miss "REPORT.PDF".
    """
    return "".join(
        f"[{char.lower()}{char.upper()}]" if char.isalpha() else char
        for char in extension
    )


class FullTextSearchClient(BaseNextcloudClient):
    """Client for searching files through the Full Text Search app."""

    app_name = "fulltextsearch"

    SEARCH_PATH = "/apps/fulltextsearch/v1/remote"

    async def search_files(
        self,
        query: str,
        *,
        extension: str = "",
        match: FullTextSearchMatch = "all",
        limit: int = 20,
        page: int = 1,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Search the ``files`` provider.

        Returns the total number of hits and the documents of the requested page.
        """
        options: dict[str, Any] = {}
        if extension:
            options["files_extension"] = _case_insensitive(extension)
        if match != "all":
            options["in"] = [match]

        request = {
            "providers": ["files"],
            "search": query,
            "page": page,
            "size": limit,
            "options": options,
            "tags": [],
            "subtags": [],
            "parts": [],
        }
        response = await self._make_request(
            "GET",
            self.SEARCH_PATH,
            params={"request": json.dumps(request)},
            headers={"Accept": "application/json"},
        )
        payload = response.json()
        if payload.get("status") != 1:
            detail = payload.get("message") or payload.get("exception") or "unknown"
            raise FullTextSearchError(str(detail))

        for result in payload.get("result") or []:
            if result.get("provider", {}).get("id") == "files":
                return result["meta"]["total"], result.get("documents") or []
        return 0, []
