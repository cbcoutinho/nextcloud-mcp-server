import logging
from email.utils import parsedate_to_datetime

from httpx import (
    AsyncBaseTransport,
    AsyncClient,
    Auth,
    BasicAuth,
    Request,
    Response,
    Timeout,
)

from ..controllers.notes_search import NotesSearchController
from ..http import nextcloud_httpx_transport
from .base import STREAMING_REQUEST_EXTENSION
from .calendar import CalendarClient
from .collectives import CollectivesClient
from .contacts import ContactsClient
from .cookbook import CookbookClient
from .deck import DeckClient
from .groups import GroupsClient
from .mail import MailClient
from .news import NewsClient
from .notes import NotesClient
from .sharing import SharingClient
from .tables import TablesClient
from .talk import TalkClient
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
    """Log the response body at DEBUG, without ever consuming a streamed one.

    Two bugs lived in the three lines this replaces, and together they defeated
    the entire streamed-download path in production:

    1. ``aread()`` ran **unconditionally**, while only the logging was gated by
       DEBUG. Every response in the app was therefore fully buffered and
       stringified regardless of log level, purely to serve a line nobody saw.
    2. httpx invokes response hooks BEFORE the body is fetched, so on a streamed
       GET that ``aread()`` pulled the whole document into memory and left the
       caller's ``aiter_bytes()`` loop with nothing to stream. A 1 GB ingest
       download became a 1 GB buffer plus its decoded ``.text`` copy -- ~3.6x the
       file size resident -- which OOMKilled the fast-tier ingest workers.

    So: bail out before the read when DEBUG is off, and never read a request the
    caller marked as streaming.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    if response.request.extensions.get(STREAMING_REQUEST_EXTENSION):
        # Reading here would consume the stream the caller is about to iterate.
        logger.debug("Response [%s] <streaming; body not read>", response.status_code)
        return
    await response.aread()
    logger.debug("Response [%s] %s", response.status_code, response.text)


def _normalise_search_result(item: dict) -> dict:
    """Normalise a webdav.search_files item to the get_files_by_tag shape."""
    path = item.get("path", "")
    if path and not path.startswith("/"):
        path = "/" + path

    last_modified_timestamp = item.get("last_modified_timestamp")
    last_modified = item.get("last_modified")
    if last_modified_timestamp is None and last_modified:
        try:
            last_modified_timestamp = int(
                parsedate_to_datetime(last_modified).timestamp()
            )
        except (TypeError, ValueError):
            last_modified_timestamp = None

    file_id = item.get("file_id") if item.get("file_id") is not None else item.get("id")

    return {
        "id": file_id,
        "path": path,
        "name": item.get("name") or (path.rsplit("/", 1)[-1] if path else ""),
        "size": item.get("size", 0),
        "content_type": item.get("content_type", ""),
        "last_modified": last_modified,
        "last_modified_timestamp": last_modified_timestamp,
        "etag": item.get("etag"),
        "is_directory": item.get("is_directory", False),
    }


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

    def __init__(
        self,
        base_url: str,
        username: str,
        auth: Auth | None = None,
        *,
        auth_username: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ):
        # ``username`` is the Nextcloud UID and DAV path fallback. Discovery can
        # replace that fallback with the canonical principal id when Nextcloud
        # exposes a different DAV identity. ``auth_username`` is the credential
        # identity Nextcloud authenticates the app password against (the
        # loginName), which differs from the UID for OIDC-provisioned users.
        # Defaults to ``username`` so single-user and OAuth modes are unchanged.
        # Callers pass the matching ``auth=BasicAuth(auth_username, ...)`` for
        # the httpx leg; ``auth_username`` is threaded to the CalDAV client,
        # which builds its own auth object from the raw credential.
        self.username = username
        auth_username = auth_username or username
        self._client = AsyncClient(
            base_url=base_url,
            auth=auth,
            transport=AsyncDisableCookieTransport(nextcloud_httpx_transport()),
            event_hooks={"request": [log_request], "response": [log_response]},
            timeout=Timeout(timeout=30, connect=5),
        )

        # Initialize app clients
        self.notes = NotesClient(self._client, username)
        self.webdav = WebDAVClient(self._client, username)
        self.tables = TablesClient(self._client, username)
        # CalendarClient takes raw credentials so caldav (which uses niquests as
        # its preferred backend in v3.x) builds a backend-compatible auth object
        # itself — passing httpx.BasicAuth here breaks under niquests (#731).
        self.calendar = CalendarClient(
            base_url,
            username,
            auth_username=auth_username,
            password=password,
            token=token,
        )
        self.contacts = ContactsClient(self._client, username)
        self.cookbook = CookbookClient(self._client, username)
        self.collectives = CollectivesClient(self._client, username)
        self.deck = DeckClient(self._client, username)
        self.news = NewsClient(self._client, username)
        self.mail = MailClient(self._client, username)
        self.talk = TalkClient(self._client, username)
        self.users = UsersClient(self._client, username)
        self.groups = GroupsClient(self._client, username)
        self.sharing = SharingClient(self._client, username)
        self.webhooks = WebhooksClient(self._client, username)

        # Initialize controllers
        self._notes_search = NotesSearchController()

    @classmethod
    def from_env(cls):
        logger.info("Creating NC Client using settings + env vars")

        # NEXTCLOUD_HOST is read via settings (dynaconf) so a settings.toml-only
        # value is honoured; the credentials remain env/Secret-sourced.
        from nextcloud_mcp_server.config import cfg, get_settings  # noqa: PLC0415

        host = get_settings().nextcloud_host
        if not host:
            raise ValueError("NEXTCLOUD_HOST must be set to build a client from env")
        username = cfg("NEXTCLOUD_USERNAME")
        password = cfg("NEXTCLOUD_PASSWORD")
        # Pass username to constructor
        return cls(
            base_url=host,
            username=username,
            auth=BasicAuth(username, password),
            password=password,
        )

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
        from ..auth import BearerAuth  # noqa: PLC0415

        logger.info("Creating NC Client for user '%s' using OAuth token", username)
        return cls(
            base_url=base_url,
            username=username,
            auth=BearerAuth(token),
            token=token,
        )

    async def capabilities(self):
        response = await self._client.get(
            "/ocs/v2.php/cloud/capabilities",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()

        return response.json()

    async def get_enabled_apps(self) -> set[str]:
        """Return the set of app ids enabled for the authenticated user.

        Uses the per-user core navigation endpoint, which lists only apps the
        current user can access (respecting group restrictions). The vector
        scanner uses this to skip polling apps the user lacks, which would 404
        and flood tenant logs. Preferred over ``/cloud/capabilities`` because
        the News app advertises no capability and so never appears there.
        """
        response = await self._client.get(
            "/ocs/v2.php/core/navigation/apps",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
        # ``X or {}``/``or []`` (not ``.get(k, default)``) so a present-but-null
        # ``ocs``/``data`` (``{"ocs": null}``) coerces to empty instead of
        # raising AttributeError on ``None.get``.
        ocs = data.get("ocs") or {}
        # A 200 carrying ``meta.status != "ok"`` is an OCS-level failure (auth /
        # permission edge cases) that ``raise_for_status`` can't see. Raise so
        # the scanner's ``_get_enabled_apps_or_none`` catches it and falls back
        # to scanning every app, rather than silently gating all apps off for a
        # cycle on an empty ``data``. Tolerate a missing/empty meta (our own
        # mocks, and any envelope that omits it).
        status = (ocs.get("meta") or {}).get("status")
        if status and status != "ok":  # falsy (missing/None/"") tolerated
            raise ValueError(f"OCS navigation returned status={status!r}")
        entries = ocs.get("data") or []
        enabled: set[str] = set()
        for entry in entries:
            # ``app`` is the canonical app id; ``id`` matches it for the apps we
            # gate. Union both so an unexpected nav-entry shape never hides an
            # enabled app — a false "disabled" would skip real indexing.
            for key in ("app", "id"):
                value = entry.get(key)
                if value:
                    enabled.add(value)
        return enabled

    async def notes_search_notes(self, *, query: str):
        """Search notes using token-based matching with relevance ranking."""
        all_notes = self.notes.get_all_notes()
        return await self._notes_search.search_notes(all_notes, query)

    async def find_files_by_tag(
        self, tag_name: str, mime_type_filter: str | None = None
    ) -> list[dict]:
        """Return files carrying ``tag_name``, expanding tagged folders into matching descendants when ``mime_type_filter`` is set."""
        tag = await self.webdav.get_tag_by_name(tag_name)
        if not tag:
            logger.debug("Tag %r not found, returning empty list", tag_name)
            return []

        items = await self.webdav.get_files_by_tag(tag["id"])
        if not items:
            logger.debug("No items found with tag %r", tag_name)
            return []

        logger.debug(
            "Found %d directly-tagged item(s) with tag %r", len(items), tag_name
        )

        # Split into directly-tagged files vs tagged directories.
        by_id: dict[int, dict] = {}
        tagged_dirs: list[dict] = []
        for item in items:
            if item.get("is_directory"):
                tagged_dirs.append(item)
                continue
            if mime_type_filter and not item.get("content_type", "").startswith(
                mime_type_filter
            ):
                continue
            file_id = item.get("id")
            if file_id is None:
                continue
            by_id[file_id] = item

        # Expand each tagged directory into its descendant files matching
        # the MIME filter. Skip when no MIME filter is set — see docstring.
        if mime_type_filter and tagged_dirs:
            for dir_info in tagged_dirs:
                dir_path = dir_info.get("path", "").strip("/")
                try:
                    # find_all_by_type pages past Nextcloud's default ~100-result
                    # SEARCH page so every tagged-folder descendant is discovered;
                    # find_by_type would silently cap a large folder.
                    descendants = await self.webdav.find_all_by_type(
                        mime_type_filter, scope=dir_path
                    )
                except Exception as e:
                    logger.warning(
                        "Tag-based directory walk failed for %r (tag %r): %s. "
                        "Skipping this folder and all its descendants — those "
                        "files will NOT be indexed until the walk succeeds.",
                        dir_path,
                        tag_name,
                        e,
                    )
                    continue

                added = 0
                for d in descendants:
                    if d.get("is_directory"):
                        continue
                    file_id = (
                        d.get("file_id")
                        if d.get("file_id") is not None
                        else d.get("id")
                    )
                    if file_id is None:
                        continue
                    if file_id in by_id:
                        # Directly-tagged entry already wins; keeps the
                        # canonical shape from get_files_by_tag.
                        continue
                    by_id[file_id] = _normalise_search_result(d)
                    added += 1

                logger.debug(
                    "Tag %r: directory %r expanded to %d descendant %s file(s)",
                    tag_name,
                    dir_path,
                    added,
                    mime_type_filter,
                )

        files = list(by_id.values())
        if mime_type_filter:
            logger.info(
                "Returning %d file(s) with tag %r (mime_type=%s, "
                "%d directly-tagged folder(s) expanded)",
                len(files),
                tag_name,
                mime_type_filter,
                len(tagged_dirs),
            )
        else:
            logger.info("Returning %d file(s) with tag %r", len(files), tag_name)
        return files

    def _get_webdav_base_path(self) -> str:
        """Helper to get the base WebDAV path for the authenticated user."""
        return self.webdav._get_webdav_base_path()

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
