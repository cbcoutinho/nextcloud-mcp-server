"""Base client for Nextcloud operations with shared authentication."""

import logging
import random
import time
import xml.etree.ElementTree as ET
from abc import ABC
from contextlib import asynccontextmanager
from functools import wraps
from urllib.parse import unquote

import anyio
from httpx import (
    AsyncClient,
    HTTPError,
    HTTPStatusError,
    RequestError,
    Response,
    codes,
)

from nextcloud_mcp_server.client.dav_errors import enrich_dav_error
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


#: Attempts allowed for a rate-limited (429) call, and the fixed wait between
#: them. 429 means "slow down", so the wait is long and flat.
MAX_RETRIES = 5
RATE_LIMIT_BACKOFF_SECONDS = 5

#: Retries allowed for a lock-contended (423) call, and the base backoff.
#:
#: 423 is a different failure from 429 and wants a different policy. Nextcloud
#: raises ``OCP\Lock\LockedException`` when it cannot upgrade a shared lock to
#: an exclusive one. Observed in CI as a note create and a concurrent note
#: LIST both calling ``mkdir`` on the same Notes folder: each holds a shared
#: lock on the path and each wants to upgrade, so neither can.
#:
#: **The jitter is the load-bearing part, not the doubling.** With a fixed
#: schedule both callers -- they are both this client, since the vector-sync
#: scanner reads the same user's notes -- back off by the same amount and
#: retry at the same instant, so they re-collide on every attempt. That is not
#: a hypothesis: a CI run showed the two requests failing at four identical
#: timestamps in a row before the budget ran out. Randomising the wait
#: desynchronises them so one acquires the exclusive lock and the other
#: follows.
#:
#: The budget is deliberately small and the original 423 is re-raised once it
#: is spent: a lock held by a long upload or an open editing session is a real
#: refusal, and this must never turn one into an unbounded wait.
LOCK_MAX_RETRIES = 3
LOCK_BACKOFF_SECONDS = 0.5


def _lock_backoff(attempt: int) -> float:
    """Jittered backoff for the *attempt*-th (1-based) lock retry.

    Equal jitter: half the doubling schedule is fixed, half is random, so the
    wait stays in ``[base * 2**(n-1) / 2, base * 2**(n-1)]``. Full jitter
    (uniform from zero) would break lockstep too, but lets a retry fire almost
    immediately, which is the opposite of what a contended lock needs.
    """
    ceiling = LOCK_BACKOFF_SECONDS * 2 ** (attempt - 1)
    # Not a security decision -- this only decides how long to wait before
    # retrying a file lock, and predictability costs nothing here.
    return ceiling / 2 + random.uniform(0, ceiling / 2)  # NOSONAR(python:S2245)


def retry_on_429(func):
    """Retry the two Nextcloud responses that mean "not now" rather than "no".

    The `func` is assumed to be a method that is similar to `httpx.Client.get`,
    and returns an `httpx.Response` object.

    * **429 Too Many Requests** -- the server is rate limiting. Waits
      ``RATE_LIMIT_BACKOFF_SECONDS`` and retries, up to ``MAX_RETRIES``
      attempts, then raises ``RuntimeError``.
    * **423 Locked** -- another request holds a lock on the path. Retries
      ``LOCK_MAX_RETRIES`` times with a short *jittered* backoff (see
      :func:`_lock_backoff`), then re-raises the original ``HTTPStatusError``
      so a genuinely locked resource still surfaces as 423 rather than as a
      retry-exhausted error.

    The two budgets are counted separately: a lock wait is not a rate-limit
    attempt, and spending one on the other would make either failure arrive
    early.

    (The name predates the 423 handling and is kept because every client
    imports it.)
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        retries = 0
        lock_retries = 0

        while True:
            try:
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
                    await anyio.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                    # Sleep first, then give up: preserves the original
                    # accounting, where the last attempt also waited.
                    if retries >= MAX_RETRIES:
                        logger.warning("All API call retries failed")
                        raise RuntimeError(
                            f"Maximum number of retries ({MAX_RETRIES}) exceeded "
                            "without success"
                        ) from e
                elif e.response.status_code == codes.LOCKED:
                    if lock_retries >= LOCK_MAX_RETRIES:
                        logger.warning(
                            "423 Locked persisted after %s retries: %s",
                            lock_retries,
                            e,
                        )
                        raise
                    lock_retries += 1
                    # A lock wait is not a rate-limit attempt.
                    retries -= 1
                    # One value, used by both the log line and the sleep. It
                    # is random, so recomputing it for the log would print a
                    # wait that never happened.
                    delay = _lock_backoff(lock_retries)
                    logger.warning(
                        "423 Locked, retrying in %.2fs (attempt %s of %s): %s",
                        delay,
                        lock_retries,
                        LOCK_MAX_RETRIES,
                        e,
                    )
                    if len(args) > 0 and hasattr(args[0], "app_name"):
                        record_nextcloud_api_retry(app=args[0].app_name, reason="423")
                    await anyio.sleep(delay)
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
                # Same typing as the buffered path. The body is unread here (the
                # status is raised inside the stream context), so this degrades
                # to status-only typing -- a 507 download still arrives as
                # DavInsufficientStorage, just without the server's wording.
                enriched = enrich_dav_error(e)
                if enriched is not e:
                    raise enriched from e
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
    async def _make_request(self, method: str, url: str, **kwargs) -> Response:
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

            # Attach the server's own explanation before re-raising. DAV replies
            # name the concrete failure in the body (Sabre s:exception/s:message)
            # and 412/423/507 become distinguishable types. Non-DAV responses are
            # returned unchanged, and the result still subclasses HTTPStatusError,
            # so every existing handler -- including retry_on_429's status check
            # around this method -- is unaffected.
            if isinstance(e, HTTPStatusError):
                enriched = enrich_dav_error(e)
                # ``enrich_dav_error`` hands back the *same* object when there is
                # no DAV document to add, and ``raise e from e`` would set
                # ``e.__cause__ = e`` -- an exception caused by itself. Re-raise
                # it plainly instead, so ``from`` always means a real wrapping.
                if enriched is not e:
                    raise enriched from e
                raise

            # Re-raise the exception
            raise
