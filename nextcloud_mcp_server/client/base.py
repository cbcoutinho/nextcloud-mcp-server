"""Base client for Nextcloud operations with shared authentication."""

import logging
import time
from abc import ABC
from functools import wraps

from httpx import AsyncClient, HTTPStatusError, RequestError, codes

from nextcloud_mcp_server.observability.metrics import (
    record_nextcloud_api_call,
    record_nextcloud_api_retry,
)
from nextcloud_mcp_server.observability.tracing import trace_nextcloud_api_call

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
                        f"429 Client Error: Too Many Requests, Number of attempts: {retries}"
                    )
                    # Record retry metric (extract app name from args if available)
                    if len(args) > 0 and hasattr(args[0], "app_name"):
                        record_nextcloud_api_retry(app=args[0].app_name, reason="429")
                    time.sleep(5)
                elif e.response.status_code == 404:
                    # 404 errors are often expected (e.g., checking if attachments exist)
                    # Log as debug instead of warning
                    logger.debug(
                        f"HTTPStatusError {e.response.status_code}: {e}, Number of attempts: {retries}"
                    )
                    raise
                else:
                    logger.warning(
                        f"HTTPStatusError {e.response.status_code}: {e}, Number of attempts: {retries}"
                    )
                    raise
            except RequestError as e:
                logger.warning(
                    f"RequestError {e.request.url}: {e}, Number of attempts: {retries}"
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

    def _get_webdav_base_path(self) -> str:
        """Helper to get the base WebDAV path for the authenticated user."""
        return f"/remote.php/dav/files/{self.username}"

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
        logger.debug(f"Making {method} request to {url}")

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
