import logging
import os

from httpx import (
    AsyncBaseTransport,
    AsyncClient,
    AsyncHTTPTransport,
    Auth,
    BasicAuth,
    Request,
    Response,
    Timeout,
)

from ..controllers.notes_search import NotesSearchController
from .calendar import CalendarClient
from .contacts import ContactsClient
from .cookbook import CookbookClient
from .deck import DeckClient
from .groups import GroupsClient
from .news import NewsClient
from .notes import NotesClient
from .sharing import SharingClient
from .tables import TablesClient
from .users import UsersClient
from .webdav import WebDAVClient
from .webhooks import WebhooksClient

logger = logging.getLogger(__name__)


async def log_request(request: Request):
    logger.debug(
        "Request event hook: %s %s - Waiting for content",
        request.method,
        request.url,
    )
    logger.debug("Request body: %s", request.content)
    logger.debug("Headers: %s", request.headers)


async def log_response(response: Response):
    await response.aread()
    logger.debug("Response [%s] %s", response.status_code, response.text)


class AsyncDisableCookieTransport(AsyncBaseTransport):
    """This Transport disable cookies from accumulating in the httpx AsyncClient

    Thanks to: https://github.com/encode/httpx/issues/2992#issuecomment-2133258994
    """

    def __init__(self, transport: AsyncBaseTransport):
        self.transport = transport

    async def handle_async_request(self, request: Request) -> Response:
        response = await self.transport.handle_async_request(request)
        response.headers.pop("set-cookie", None)
        return response


class NextcloudClient:
    """Main Nextcloud client that orchestrates all app clients."""

    def __init__(self, base_url: str, username: str, auth: Auth | None = None):
        self.username = username
        self._client = AsyncClient(
            base_url=base_url,
            auth=auth,
            transport=AsyncDisableCookieTransport(AsyncHTTPTransport()),
            event_hooks={"request": [log_request], "response": [log_response]},
            timeout=Timeout(timeout=30, connect=5),
        )

        # Initialize app clients
        self.notes = NotesClient(self._client, username)
        self.webdav = WebDAVClient(self._client, username)
        self.tables = TablesClient(self._client, username)
        self.calendar = CalendarClient(
            base_url, username, auth
        )  # Uses AsyncDavClient internally
        self.contacts = ContactsClient(self._client, username)
        self.cookbook = CookbookClient(self._client, username)
        self.deck = DeckClient(self._client, username)
        self.news = NewsClient(self._client, username)
        self.users = UsersClient(self._client, username)
        self.groups = GroupsClient(self._client, username)
        self.sharing = SharingClient(self._client, username)
        self.webhooks = WebhooksClient(self._client, username)

        # Initialize controllers
        self._notes_search = NotesSearchController()

    @classmethod
    def from_env(cls):
        logger.info("Creating NC Client using env vars")

        host = os.environ["NEXTCLOUD_HOST"]
        username = os.environ["NEXTCLOUD_USERNAME"]
        password = os.environ["NEXTCLOUD_PASSWORD"]
        # Pass username to constructor
        return cls(base_url=host, username=username, auth=BasicAuth(username, password))

    @classmethod
    def from_token(cls, base_url: str, token: str, username: str):
        """Create NextcloudClient with OAuth bearer token.

        Args:
            base_url: Nextcloud base URL
            token: OAuth access token
            username: Nextcloud username

        Returns:
            NextcloudClient configured with bearer token authentication
        """
        from ..auth import BearerAuth

        logger.info(f"Creating NC Client for user '{username}' using OAuth token")
        return cls(base_url=base_url, username=username, auth=BearerAuth(token))

    async def capabilities(self):
        response = await self._client.get(
            "/ocs/v2.php/cloud/capabilities",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()

        return response.json()

    async def notes_search_notes(self, *, query: str):
        """Search notes using token-based matching with relevance ranking."""
        all_notes = self.notes.get_all_notes()
        return await self._notes_search.search_notes(all_notes, query)

    async def find_files_by_tag(
        self, tag_name: str, mime_type_filter: str | None = None
    ) -> list[dict]:
        """Find files by system tag name, optionally filtered by MIME type.

        This method coordinates tag lookup and file retrieval via WebDAV:
        1. Look up the tag ID by name
        2. Get all files with that tag (via REPORT with full metadata)
        3. Optionally filter by MIME type

        Args:
            tag_name: Name of the system tag to search for (e.g., "vector-index")
            mime_type_filter: Optional MIME type filter (e.g., "application/pdf")

        Returns:
            List of file dictionaries with WebDAV properties (path, size, content_type, etc.)

        Raises:
            RuntimeError: If tag lookup or file query fails

        Examples:
            # Find all files with "vector-index" tag
            files = await nc_client.find_files_by_tag("vector-index")

            # Find only PDFs with the tag
            pdfs = await nc_client.find_files_by_tag("vector-index", "application/pdf")
        """
        # Look up tag by name using WebDAV
        tag = await self.webdav.get_tag_by_name(tag_name)
        if not tag:
            logger.debug(f"Tag '{tag_name}' not found, returning empty list")
            return []

        # Get files with this tag (returns full file info from REPORT)
        files = await self.webdav.get_files_by_tag(tag["id"])
        if not files:
            logger.debug(f"No files found with tag '{tag_name}'")
            return []

        logger.debug(f"Found {len(files)} files with tag '{tag_name}'")

        # Apply MIME type filter if specified
        if mime_type_filter:
            filtered_files = [
                f
                for f in files
                if f.get("content_type", "").startswith(mime_type_filter)
            ]
            logger.info(
                f"Returning {len(filtered_files)} files with tag '{tag_name}' (filtered by {mime_type_filter})"
            )
            return filtered_files

        logger.info(f"Returning {len(files)} files with tag '{tag_name}'")
        return files

    def _get_webdav_base_path(self) -> str:
        """Helper to get the base WebDAV path for the authenticated user."""
        return f"/remote.php/dav/files/{self.username}"

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - closes all clients."""
        await self.close()
        return False  # Don't suppress exceptions

    async def close(self):
        """Close the HTTP client and CalDAV client."""
        await self._client.aclose()
        await self.calendar.close()
