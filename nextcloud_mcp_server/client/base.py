"""Base client for Nextcloud operations with shared authentication."""

import logging
import time
import xml.etree.ElementTree as ET
from abc import ABC
from contextlib import asynccontextmanager
from functools import wraps
from urllib.parse import unquote

import anyio
from httpx import AsyncClient, HTTPError, HTTPStatusError, RequestError, codes

from nextcloud_mcp_server.observability.metrics import (
    record_nextcloud_api_call,
    record_nextcloud_api_retry,
)
from nextcloud_mcp_server.observability.tracing import trace_nextcloud_api_call

#: Marks a request whose body the caller intends to consume incrementally.
#:
#: httpx runs response event hooks BEFORE the body is fetched, so a hook that
#: calls ``aread()`` pulls the whole body into memory and leaves nothing for the
#: caller's ``aiter_bytes()`` loop -- silently turning a streamed download into a
#: buffered one. ``log_response`` checks for this marker and skips reading.
STREAMING_REQUEST_EXTENSION = "nextcloud_mcp_streaming"

logger = logging.getLogger(__name__)


def retry_on_429(func):
    """This decorator handles the 429 response from REST APIs

    The `func` is assumed to be a method that is similar to `httpx.Client.get`,
    and returns an `httpx.Response` object. In the case of `Too Many Requests` HTTP
    response, the function will wait for a couple of seconds and retry the request.
    """

    MAX_RETRIES = 5

    @wraps(func)
    async def wrapper(*args, **kwargs):
        retries = 0

        while retries < MAX_RETRIES:
            try:
                # Make GET API call
                retries += 1
                response = await func(*args, **kwargs)
                break

            except HTTPStatusError as e:
                # If we get a '429 Client Error: Too Many Requests'
                # error we wait a couple of seconds and do a retry
                if e.response.status_code == codes.TOO_MANY_REQUESTS:
                    logger.warning(
                        "429 Client Error: Too Many Requests, Number of attempts: %s",
                        retries,
                    )
                    # Record retry metric (extract app name from args if available)
                    if len(args) > 0 and hasattr(args[0], "app_name"):
                        record_nextcloud_api_retry(app=args[0].app_name, reason="429")
                    await anyio.sleep(5)
                elif e.response.status_code == 404:
                    # 404 errors are often expected (e.g., checking if attachments exist)
                    # Log as debug instead of warning
                    logger.debug(
                        "HTTPStatusError %s: %s, Number of attempts: %s",
                        e.response.status_code,
                        e,
                        retries,
                    )
                    raise
                else:
                    logger.warning(
                        "HTTPStatusError %s: %s, Number of attempts: %s",
                        e.response.status_code,
                        e,
                        retries,
                    )
                    raise
            except RequestError as e:
                logger.warning(
                    "RequestError %s: %s, Number of attempts: %s",
                    e.request.url,
                    e,
                    retries,
                )
                raise

        # If for loop ends without break statement
        else:
            logger.warning("All API call retries failed")
            raise RuntimeError(
                f"Maximum number of retries ({MAX_RETRIES}) exceeded without success"
            )

        return response

    return wrapper


class BaseNextcloudClient(ABC):
    """Base class for all Nextcloud app clients."""

    # Subclasses should set this to identify the app for metrics/tracing
    app_name: str = "unknown"

    def __init__(self, http_client: AsyncClient, username: str):
        """Initialize with shared HTTP client and username.

        Args:
            http_client: Authenticated AsyncClient instance
            username: Nextcloud username for WebDAV operations
        """
        self._client = http_client
        self.username = username
        self._principal_id: str | None = None
        self._principal_discovered = False

    def _get_webdav_base_path(self) -> str:
        """Helper to get the base WebDAV path for the authenticated user."""
        return f"/remote.php/dav/files/{self._principal_or_username()}"

    async def _ensure_principal_id(self) -> None:
        """Discover the canonical DAV principal id via current-user-principal."""
        if getattr(self, "_principal_discovered", False):
            return

        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:"><d:prop>'
            "<d:current-user-principal/>"
            "</d:prop></d:propfind>"
        )

        try:
            response = await self._make_request(
                "PROPFIND",
                "/remote.php/dav/",
                content=body,
                headers={"Depth": "0", "Content-Type": "application/xml"},
            )
            root = ET.fromstring(response.content)
            href = None
            for element in root.iter():
                if element.tag.split("}")[-1] != "current-user-principal":
                    continue
                for child in element.iter():
                    if child.tag.split("}")[-1] == "href" and child.text:
                        href = child.text.strip()
                        break
                if href:
                    break

            if not href:
                logger.warning(
                    "DAV principal discovery returned no href; using username path"
                )
                return

            principal_id = unquote(href.rstrip("/").split("/")[-1])
            if not principal_id:
                logger.warning(
                    "DAV principal discovery returned an empty principal id; "
                    "using username path"
                )
                return

            self._principal_id = principal_id
            self._principal_discovered = True
        except (HTTPError, ET.ParseError, ValueError) as e:
            logger.warning("DAV principal discovery failed; using username path: %s", e)

    def _principal_or_username(self) -> str:
        """Return the discovered DAV principal id, falling back to username."""
        return getattr(self, "_principal_id", None) or self.username

    @staticmethod
    def _resolve_url(url: str) -> str:
        """Prefix bare ``/apps/...`` paths with ``/index.php``.

        Pretty URLs (URL rewriting that strips ``index.php``) are an opt-in
        Nextcloud feature; without them, ``/apps/<app>/...`` returns 404 — see
        issue #732. ``/index.php/apps/<app>/...`` is the universal entry point
        and works on every Nextcloud install regardless of web-server config,
        so we route all app-API calls through it. ``/remote.php/dav/...`` and
        ``/ocs/...`` have their own dedicated entry points and are unaffected.
        """
        if url.startswith("/apps/"):
            return "/index.php" + url
        return url

    @asynccontextmanager
    async def _stream_request(self, method: str, url: str, **kwargs):
        """Streaming sibling of :meth:`_make_request`, yielding an unread Response.

        Shares ``_resolve_url``, tracing and the API-call metric, so a streamed
        download is not a second, untraced transport path. The body is NOT read
        here -- the caller consumes ``aiter_bytes()`` inside the ``async with``.

        ``retry_on_429`` deliberately does not apply: it re-invokes a coroutine
        that returns a fully-read Response, and a partially-consumed stream
        cannot be replayed. Retry only the connect+status phase instead, before
        any body byte is yielded; a mid-body failure surfaces as the retryable
        transport error it already is.
        """
        url = self._resolve_url(url)
        logger.debug("Making streaming %s request to %s", method, url)

        # Tell the response event hook to keep its hands off this body. Without
        # it, log_response's aread() consumes the whole document before the
        # caller's aiter_bytes() loop runs -- the download is then buffered, not
        # streamed, and peak memory scales with file size again.
        stream_extensions = {
            **kwargs.pop("extensions", {}),
            STREAMING_REQUEST_EXTENSION: True,
        }

        max_retries = 5
        for attempt in range(1, max_retries + 1):
            start_time = time.time()
            # Status is recorded in a finally so the request is metered however
            # the block ends. Without that, a failure raised by the CALLER's body
            # loop (OversizeDownload, or the short-read RemoteProtocolError) is
            # neither an HTTPStatusError nor a normal return, so the request went
            # entirely unrecorded on mcp_nextcloud_api_requests_total.
            status_code = 0
            try:
                with trace_nextcloud_api_call(
                    app=self.app_name, method=method, path=url
                ):
                    async with self._client.stream(
                        method, url, extensions=stream_extensions, **kwargs
                    ) as response:
                        status_code = response.status_code
                        # Raised inside the stream context so the connection is
                        # released before the 429 handler sleeps and retries.
                        response.raise_for_status()
                        yield response
                return
            except HTTPStatusError as e:
                status_code = e.response.status_code
                if status_code == codes.TOO_MANY_REQUESTS and attempt < max_retries:
                    logger.warning(
                        "429 Too Many Requests on streaming download, attempt %s",
                        attempt,
                    )
                    record_nextcloud_api_retry(app=self.app_name, reason="429")
                    await anyio.sleep(5)
                    continue
                raise
            finally:
                # A retried 429 is metered as its own attempt, matching how
                # retry_on_429 accounts for the buffered path.
                record_nextcloud_api_call(
                    app=self.app_name,
                    method=method,
                    status_code=status_code,
                    duration=time.time() - start_time,
                )
        raise RuntimeError(
            f"Maximum number of retries ({max_retries}) exceeded without success"
        )

    @retry_on_429
    async def _make_request(self, method: str, url: str, **kwargs):
        """Common request wrapper with logging, tracing, and error handling.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional request parameters

        Returns:
            Response object
        """
        url = self._resolve_url(url)
        logger.debug("Making %s request to %s", method, url)

        # Start timer for metrics
        start_time = time.time()
        status_code = 0

        try:
            # Wrap request in trace span
            with trace_nextcloud_api_call(
                app=self.app_name,
                method=method,
                path=url,
            ):
                response = await self._client.request(method, url, **kwargs)
                status_code = response.status_code
                response.raise_for_status()

                # Record successful API call metrics
                duration = time.time() - start_time
                record_nextcloud_api_call(
                    app=self.app_name,
                    method=method,
                    status_code=status_code,
                    duration=duration,
                )

                return response

        except (HTTPStatusError, RequestError) as e:
            # Record error metrics
            if isinstance(e, HTTPStatusError):
                status_code = e.response.status_code
            else:
                status_code = 0  # Connection error, no status code

            duration = time.time() - start_time
            record_nextcloud_api_call(
                app=self.app_name,
                method=method,
                status_code=status_code,
                duration=duration,
            )

            # Re-raise the exception
            raise
