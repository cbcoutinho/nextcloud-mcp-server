"""Base client for Nextcloud operations with shared authentication."""

import logging
import time
from abc import ABC
from functools import wraps

from httpx import AsyncClient, HTTPStatusError, RequestError, codes

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
        """Common request wrapper with logging and error handling.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional request parameters

        Returns:
            Response object
        """
        logger.debug(f"Making {method} request to {url}")
        response = await self._client.request(method, url, **kwargs)
        response.raise_for_status()
        return response
