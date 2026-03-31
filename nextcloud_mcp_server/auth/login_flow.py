"""Nextcloud Login Flow v2 HTTP client.

Implements the Nextcloud Login Flow v2 protocol for obtaining app passwords.
See: https://docs.nextcloud.com/server/latest/developer_manual/client_apis/LoginFlow/index.html#login-flow-v2

The flow has two steps:
1. Initiate: POST /index.php/login/v2 → returns login URL + poll endpoint/token
2. Poll: POST to poll endpoint with token → returns server URL, loginName, appPassword
"""

import logging
import ssl
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field

from nextcloud_mcp_server.http import nextcloud_httpx_client

logger = logging.getLogger(__name__)


def rewrite_url_origin(url: str, target_host: str) -> str:
    """Rewrite a URL's scheme+host+port to match target_host.

    Preserves the path, params, query, and fragment from the original URL.
    Useful for rewriting internal Docker hostnames to public-facing URLs.
    """
    parsed_url = urlparse(url)
    parsed_host = urlparse(target_host)
    return urlunparse(
        (
            parsed_host.scheme,
            parsed_host.netloc,
            parsed_url.path,
            parsed_url.params,
            parsed_url.query,
            parsed_url.fragment,
        )
    )


class LoginFlowInitResponse(BaseModel):
    """Response from initiating Login Flow v2."""

    login_url: str = Field(description="URL to present to the user for browser login")
    poll_endpoint: str = Field(description="URL to poll for flow completion")
    poll_token: str = Field(description="Token to use when polling")


class LoginFlowPollResult(BaseModel):
    """Result of polling Login Flow v2."""

    status: str = Field(description="Flow status: 'pending', 'completed', or 'expired'")
    server: str | None = Field(None, description="Nextcloud server URL (on completion)")
    login_name: str | None = Field(
        None, description="Nextcloud login name (on completion)"
    )
    app_password: str | None = Field(
        None, description="Generated app password (on completion)"
    )


class LoginFlowV2Client:
    """HTTP client for Nextcloud Login Flow v2.

    This client handles the two-step Login Flow v2 process:
    1. Initiate a flow to get a login URL for the user
    2. Poll for completion to receive the app password

    Args:
        nextcloud_host: Base URL of the Nextcloud instance
        verify_ssl: SSL verification setting (True, False, or SSLContext)
    """

    def __init__(
        self,
        nextcloud_host: str,
        verify_ssl: bool | ssl.SSLContext = True,
    ):
        self.nextcloud_host = nextcloud_host.rstrip("/")
        self.verify_ssl = verify_ssl

    async def initiate(
        self, user_agent: str = "nextcloud-mcp-server"
    ) -> LoginFlowInitResponse:
        """Initiate Login Flow v2 by sending an HTTP POST to the Nextcloud instance.

        Makes an outbound HTTP request to POST /index.php/login/v2 on the
        configured Nextcloud server to start a new login flow.

        Args:
            user_agent: User-Agent string for the app password name

        Returns:
            LoginFlowInitResponse with login URL and poll credentials

        Raises:
            httpx.HTTPStatusError: If the Nextcloud server returns an error
        """
        url = f"{self.nextcloud_host}/index.php/login/v2"

        async with nextcloud_httpx_client(
            verify=self.verify_ssl, timeout=15.0
        ) as client:
            response = await client.post(
                url,
                headers={"User-Agent": user_agent},
            )
            response.raise_for_status()
            data = response.json()

        poll_data = data.get("poll", {})

        try:
            raw_poll_endpoint = poll_data["endpoint"]
            # Nextcloud returns URLs using its internal hostname (e.g.
            # http://localhost/login/v2/poll) which may be unreachable from
            # this process. Rewrite the poll endpoint to use nextcloud_host
            # so server-side polling works across Docker networks.
            poll_endpoint = self._rewrite_to_nextcloud_host(raw_poll_endpoint)

            result = LoginFlowInitResponse(
                login_url=data["login"],
                poll_endpoint=poll_endpoint,
                poll_token=poll_data["token"],
            )
        except KeyError as e:
            raise ValueError(
                f"Malformed Login Flow v2 initiate response from Nextcloud (missing key: {e})"
            ) from e

        logger.info(f"Login Flow v2 initiated: login_url={result.login_url[:60]}...")
        return result

    def _rewrite_to_nextcloud_host(self, url: str) -> str:
        """Rewrite a URL's origin to use self.nextcloud_host.

        Nextcloud may return URLs with its internal hostname (e.g.
        http://localhost) which differs from the configured NEXTCLOUD_HOST
        (e.g. http://app:80). This replaces the scheme+host+port while
        preserving the path and query.
        """
        result = rewrite_url_origin(url, self.nextcloud_host)
        if result != url:
            logger.debug(f"Rewrote Login Flow v2 URL: {url} → {result}")
        return result

    async def poll(self, poll_endpoint: str, poll_token: str) -> LoginFlowPollResult:
        """Poll for Login Flow v2 completion by sending an HTTP POST to the Nextcloud instance.

        Makes an outbound HTTP request to the poll endpoint provided by the
        initiate response. Nextcloud returns:
        - 200 with credentials when the user completes login
        - 404 when still pending
        - Other errors for expired/invalid flows

        Args:
            poll_endpoint: URL to poll (from initiate response)
            poll_token: Token for polling (from initiate response)

        Returns:
            LoginFlowPollResult with status and optional credentials
        """
        async with nextcloud_httpx_client(
            verify=self.verify_ssl, timeout=10.0
        ) as client:
            response = await client.post(
                poll_endpoint,
                data={"token": poll_token},
            )

        if response.status_code == 200:
            data = response.json()
            logger.info(
                f"Login Flow v2 completed: server={data.get('server')}, "
                f"loginName={data.get('loginName')}"
            )
            try:
                return LoginFlowPollResult(
                    status="completed",
                    server=data["server"],
                    login_name=data["loginName"],
                    app_password=data["appPassword"],
                )
            except KeyError as e:
                raise ValueError(
                    f"Malformed Login Flow v2 poll response from Nextcloud (missing key: {e})"
                ) from e

        if response.status_code == 404:
            logger.debug("Login Flow v2 still pending")
            return LoginFlowPollResult(status="pending")

        # Any other status indicates the flow has expired or is invalid
        logger.warning(
            f"Login Flow v2 poll returned unexpected status: {response.status_code}"
        )
        return LoginFlowPollResult(status="expired")
