"""Fixtures for Login Flow v2 integration tests.

These fixtures handle the complete provisioning flow:
1. Create OAuth client for the login-flow MCP server (port 8004)
2. Obtain OAuth token via Playwright browser automation
3. Connect MCP client session with OAuth token
4. Complete Login Flow v2 provisioning (browser login → app password)
5. Run MCP tools against the provisioned session
"""

import json
import logging
import os
import secrets
import time
from typing import Any, AsyncGenerator
from urllib.parse import quote, urlparse, urlunparse

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.types import ElicitRequestParams, ElicitResult

from tests.conftest import (
    DEFAULT_FULL_SCOPES,
    _handle_oauth_consent_screen,
    create_mcp_client_session,
    get_mcp_server_resource_metadata,
)

logger = logging.getLogger(__name__)

LOGIN_FLOW_MCP_URL = "http://localhost:8004/mcp"
LOGIN_FLOW_MCP_BASE_URL = "http://localhost:8004"


@pytest.fixture(scope="session")
async def login_flow_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """Create OAuth client credentials for the login-flow MCP server (port 8004).

    Uses Dynamic Client Registration against Nextcloud's OIDC endpoint.
    The client only needs openid/profile/email scopes since Login Flow v2
    uses app passwords for Nextcloud API access, not OAuth tokens.
    """
    from nextcloud_mcp_server.auth.client_registration import (
        delete_client,
        register_client,
    )

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Login Flow tests require NEXTCLOUD_HOST")

    auth_states, callback_url = oauth_callback_server

    logger.info("Setting up OAuth client for login-flow MCP server (port 8004)...")

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await http_client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        token_endpoint = oidc_config["token_endpoint"]
        authorization_endpoint = oidc_config["authorization_endpoint"]
        registration_endpoint = oidc_config["registration_endpoint"]

    # Login flow only needs identity scopes for the MCP session;
    # we also request resource scopes so the token passes the MCP server's
    # scope validation (the server advertises these scopes).
    client_info = await register_client(
        nextcloud_url=nextcloud_host,
        registration_endpoint=registration_endpoint,
        client_name="Pytest - Login Flow Test Client",
        redirect_uris=[callback_url],
        scopes=DEFAULT_FULL_SCOPES,
        token_type="Bearer",
    )

    logger.info(f"Login Flow OAuth client ready: {client_info.client_id[:16]}...")

    yield (
        client_info.client_id,
        client_info.client_secret,
        callback_url,
        token_endpoint,
        authorization_endpoint,
    )

    # Cleanup
    try:
        await delete_client(
            nextcloud_url=nextcloud_host,
            client_id=client_info.client_id,
            registration_access_token=client_info.registration_access_token,
            client_secret=client_info.client_secret,
            registration_client_uri=client_info.registration_client_uri,
        )
        logger.info(
            f"Cleaned up Login Flow OAuth client: {client_info.client_id[:16]}..."
        )
    except Exception as e:
        logger.warning(f"Failed to clean up Login Flow OAuth client: {e}")


@pytest.fixture(scope="session")
async def login_flow_oauth_token(
    anyio_backend, browser, login_flow_oauth_client_credentials, oauth_callback_server
) -> str:
    """Obtain OAuth token for the login-flow MCP server.

    Uses Playwright browser automation to complete the OAuth flow against
    Nextcloud, obtaining a token suitable for the port 8004 MCP session.
    """
    # FIXME: Playwright browser automation has issues with the localhost
    # callback server in GitHub Actions CI. Address in a follow-up PR.
    if os.getenv("GITHUB_ACTIONS"):
        pytest.skip(
            "Login Flow tests with browser automation not supported in GitHub Actions CI"
        )

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")
    password = os.getenv("NEXTCLOUD_PASSWORD")

    if not all([nextcloud_host, username, password]):
        pytest.skip(
            "Login Flow OAuth requires NEXTCLOUD_HOST, NEXTCLOUD_USERNAME, NEXTCLOUD_PASSWORD"
        )

    auth_states, _ = oauth_callback_server
    client_id, client_secret, callback_url, token_endpoint, authorization_endpoint = (
        login_flow_oauth_client_credentials
    )

    # Fetch resource metadata from port 8004 for audience
    try:
        resource_metadata = await get_mcp_server_resource_metadata(
            LOGIN_FLOW_MCP_BASE_URL
        )
        resource_id = resource_metadata.get("resource")
    except Exception as e:
        logger.warning(f"Failed to fetch resource metadata from port 8004: {e}")
        resource_id = None

    state = secrets.token_urlsafe(32)
    scopes_encoded = quote(DEFAULT_FULL_SCOPES, safe="")

    auth_url = (
        f"{authorization_endpoint}?"
        f"response_type=code&"
        f"client_id={client_id}&"
        f"redirect_uri={quote(callback_url, safe='')}&"
        f"state={state}&"
        f"scope={scopes_encoded}"
    )
    if resource_id:
        auth_url += f"&resource={quote(resource_id, safe='')}"

    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)
        current_url = page.url

        if "/login" in current_url or "/index.php/login" in current_url:
            await page.wait_for_selector('input[name="user"]', timeout=10000)
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=60000)

        try:
            await _handle_oauth_consent_screen(page, username)
        except Exception:
            pass

        start_time = time.time()
        while state not in auth_states:
            if time.time() - start_time > 30:
                raise TimeoutError("Timeout waiting for OAuth callback")
            await anyio.sleep(0.5)

        auth_code = auth_states[state]
    finally:
        await context.close()

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        token_response = await http_client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": callback_url,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        access_token = token_data["access_token"]

    logger.info("Successfully obtained OAuth token for login-flow MCP server")
    return access_token


def _rewrite_login_flow_url(login_url: str) -> str:
    """Rewrite internal Docker URLs to host-accessible URLs.

    The MCP server runs inside Docker with NEXTCLOUD_HOST=http://app:80,
    so Login Flow v2 URLs use the internal hostname. Playwright runs on
    the host and needs localhost:8080 instead.
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST", "http://localhost:8080")
    target = urlparse(nextcloud_host)
    parsed = urlparse(login_url)
    if parsed.hostname == "app":
        parsed = parsed._replace(scheme=target.scheme, netloc=target.netloc)
    return urlunparse(parsed)


async def _complete_login_flow_v2(browser, login_url: str) -> None:
    """Complete Nextcloud Login Flow v2 in a browser.

    The full Nextcloud Login Flow v2 has these steps:
    1. "Connect to your account" page → click "Log in" button
    2. Login form → fill username/password, submit
       (if already logged in via session cookie, this step is skipped)
    3. "Account access" grant page → click "Grant access" button
    4. Password confirmation dialog → enter password, click "Confirm"
    5. "Account connected" success page

    Args:
        browser: Playwright browser instance
        login_url: URL from Login Flow v2 initiation (e.g., /login/v2/flow/...)
    """
    username = os.getenv("NEXTCLOUD_USERNAME", "admin")
    password = os.getenv("NEXTCLOUD_PASSWORD", "admin")

    # Rewrite internal Docker URL to host-accessible URL
    login_url = _rewrite_login_flow_url(login_url)

    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        logger.info(f"Opening Login Flow v2 URL: {login_url[:80]}...")
        await page.goto(login_url, wait_until="networkidle", timeout=60000)
        logger.info(f"Step 1 - Current URL: {page.url}")

        # Step 1: "Connect to your account" page - click "Log in"
        login_btn = page.get_by_role("button", name="Log in")
        try:
            await login_btn.wait_for(timeout=10000)
            await login_btn.click()
            logger.info("Clicked 'Log in' on Connect page")
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            logger.info("No 'Log in' button - may already be on login/grant page")

        logger.info(f"Step 2 - Current URL: {page.url}")

        # Step 2: Login form (only if not already logged in)
        # If the user has an active session, they skip straight to the grant page.
        user_field = page.locator('input[name="user"]')
        if await user_field.count() > 0:
            logger.info("Login form detected, filling credentials...")
            await user_field.fill(username)
            await page.locator('input[name="password"]').fill(password)
            await page.get_by_role("button", name="Log in", exact=True).click()
            await page.wait_for_load_state("networkidle", timeout=60000)
            logger.info(f"After login: {page.url}")
        else:
            logger.info("No login form - already logged in via session")

        # Step 3: "Account access" grant page - click "Grant access"
        grant_btn = page.get_by_role("button", name="Grant access")
        try:
            await grant_btn.wait_for(timeout=15000)
            await grant_btn.click()
            logger.info("Clicked 'Grant access'")
        except Exception as e:
            logger.warning(f"No Grant access button: {e}")
            await page.screenshot(path="/tmp/login_flow_no_grant.png")

        # Step 4: Password confirmation dialog
        # Nextcloud shows "Authentication required" dialog after clicking Grant access
        confirm_password = page.get_by_role("dialog").get_by_role(
            "textbox", name="Password"
        )
        try:
            await confirm_password.wait_for(timeout=10000)
            logger.info("Password confirmation dialog detected")
            await confirm_password.fill(password)

            # Wait for Confirm button to become enabled after filling password
            confirm_btn = page.get_by_role("dialog").get_by_role(
                "button", name="Confirm"
            )
            await confirm_btn.wait_for(timeout=5000)
            await confirm_btn.click()
            logger.info("Clicked 'Confirm' in password dialog")
        except Exception:
            logger.info(
                "No password confirmation dialog (may have been auto-confirmed)"
            )

        # Step 5: Wait for "Account connected" success page
        try:
            await page.get_by_text("Account connected").wait_for(timeout=15000)
            logger.info("Login Flow v2 completed: Account connected!")
        except Exception:
            # The grant may have completed without the success page being visible
            await page.wait_for_load_state("networkidle", timeout=10000)
            logger.info(f"Login Flow v2 done. Final URL: {page.url}")

    finally:
        await context.close()


@pytest.fixture(scope="session")
async def nc_mcp_login_flow_client(
    anyio_backend,
    login_flow_oauth_token: str,
    browser,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client session connected to the login-flow server (port 8004).

    This fixture:
    1. Connects to the MCP server with an OAuth token
    2. Calls nc_auth_provision_access to start Login Flow v2
    3. Completes the browser login to get an app password
    4. Calls nc_auth_check_status to finalize provisioning
    5. Yields the provisioned MCP client session

    All subsequent tool calls will use the stored app password.
    """
    # Create an elicitation callback that extracts the login URL
    # and completes the Login Flow v2 in the browser
    login_url_holder: dict[str, str] = {}

    async def elicitation_callback(
        context: Any,
        params: ElicitRequestParams,
    ) -> ElicitResult:
        """Handle elicitation from nc_auth_provision_access.

        Extracts the login URL from the elicitation message and
        completes the Login Flow v2 browser login.
        """
        message = params.message
        logger.info(f"Elicitation received: {message[:100]}...")

        # Extract login URL from elicitation message
        for line in message.split("\n"):
            stripped = line.strip()
            if stripped.startswith("http") and "/login/v2/" in stripped:
                login_url_holder["url"] = stripped
                logger.info(f"Extracted login URL: {stripped[:80]}...")
                break

        if "url" in login_url_holder:
            # Complete the Login Flow v2 in the browser
            await _complete_login_flow_v2(browser, login_url_holder["url"])

        # Return acceptance
        return ElicitResult(
            action="accept",
            content={"acknowledged": True},
        )

    async for session in create_mcp_client_session(
        url=LOGIN_FLOW_MCP_URL,
        token=login_flow_oauth_token,
        client_name="Login Flow MCP",
        elicitation_callback=elicitation_callback,
    ):
        # Step 1: Provision access via Login Flow v2
        logger.info("Starting Login Flow v2 provisioning...")
        provision_result = await session.call_tool(
            "nc_auth_provision_access",
            {"scopes": None},  # Request all scopes
        )

        provision_data = json.loads(provision_result.content[0].text)
        logger.info(f"Provision result: {provision_data.get('status')}")

        # If elicitation didn't fire (client doesn't support it),
        # extract URL from the response and complete flow manually
        if provision_data.get("status") == "login_required":
            login_url = provision_data.get("login_url")
            if login_url and "url" not in login_url_holder:
                logger.info("Completing Login Flow v2 from response URL...")
                await _complete_login_flow_v2(browser, login_url)

        # Step 2: Poll for completion
        logger.info("Polling Login Flow v2 status...")
        max_attempts = 15
        for attempt in range(max_attempts):
            status_result = await session.call_tool("nc_auth_check_status", {})
            status_data = json.loads(status_result.content[0].text)
            status = status_data.get("status")
            logger.info(f"Status check {attempt + 1}/{max_attempts}: {status}")

            if status == "provisioned":
                logger.info(
                    f"Login Flow v2 provisioned! Username: {status_data.get('username')}"
                )
                break

            if status in ("not_initiated", "error"):
                raise RuntimeError(
                    f"Login Flow v2 failed: {status_data.get('message')}"
                )

            await anyio.sleep(2)
        else:
            raise TimeoutError(
                f"Login Flow v2 did not complete after {max_attempts} attempts"
            )

        yield session
