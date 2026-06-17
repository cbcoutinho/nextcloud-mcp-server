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
    DEFAULT_READ_SCOPES,
    DEFAULT_WRITE_SCOPES,
    _get_oauth_token_with_scopes,
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

    logger.info("Login Flow OAuth client ready: %s...", client_info.client_id[:16])

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
            "Cleaned up Login Flow OAuth client: %s...", client_info.client_id[:16]
        )
    except Exception as e:
        logger.warning("Failed to clean up Login Flow OAuth client: %s", e)


@pytest.fixture(scope="session")
async def login_flow_oauth_token(
    anyio_backend, browser, login_flow_oauth_client_credentials, oauth_callback_server
) -> str:
    """Obtain OAuth token for the login-flow MCP server.

    Uses Playwright browser automation to complete the OAuth flow against
    Nextcloud, obtaining a token suitable for the port 8004 MCP session.
    """
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
        logger.warning("Failed to fetch resource metadata from port 8004: %s", e)
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
        logger.info("Opening Login Flow v2 URL: %s...", login_url[:80])
        await page.goto(login_url, wait_until="networkidle", timeout=60000)
        logger.info("Step 1 - Current URL: %s", page.url)

        # Step 1: "Connect to your account" page - click "Log in".
        # exact=True is required: NC33's connect page also renders an
        # "Alternative log in using app password" button, and a non-exact
        # "Log in" name substring-matches both -> Playwright strict-mode error
        # that was silently swallowed below, leaving the flow stuck on the
        # connect page (every login-flow test then times out "Login Flow v2 did
        # not complete"). NC32 has a single match, so exact=True is safe there.
        login_btn = page.get_by_role("button", name="Log in", exact=True)
        try:
            await login_btn.wait_for(timeout=10000)
            await login_btn.click()
            logger.info("Clicked 'Log in' on Connect page")
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            logger.info("No 'Log in' button - may already be on login/grant page")

        logger.info("Step 2 - Current URL: %s", page.url)

        # Step 2: Login form (only if not already logged in)
        # If the user has an active session, they skip straight to the grant page.
        user_field = page.locator('input[name="user"]')
        if await user_field.count() > 0:
            logger.info("Login form detected, filling credentials...")
            await user_field.fill(username)
            await page.locator('input[name="password"]').fill(password)
            await page.get_by_role("button", name="Log in", exact=True).click()
            await page.wait_for_load_state("networkidle", timeout=60000)
            logger.info("After login: %s", page.url)
        else:
            logger.info("No login form - already logged in via session")

        # Step 3: "Account access" grant page - click "Grant access"
        grant_btn = page.get_by_role("button", name="Grant access")
        try:
            await grant_btn.wait_for(timeout=15000)
            await grant_btn.click()
            logger.info("Clicked 'Grant access'")
        except Exception as e:
            logger.warning("No Grant access button: %s", e)
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
            logger.info("Login Flow v2 done. Final URL: %s", page.url)

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
        logger.info("Elicitation received: %s...", message[:100])

        # Extract login URL from elicitation message
        for line in message.split("\n"):
            stripped = line.strip()
            if stripped.startswith("http") and "/login/v2/" in stripped:
                login_url_holder["url"] = stripped
                logger.info("Extracted login URL: %s...", stripped[:80])
                break

        if "url" in login_url_holder:
            # Complete the Login Flow v2 in the browser
            await _complete_login_flow_v2(browser, login_url_holder["url"])

        # Return acceptance
        return ElicitResult(
            action="accept",
            content={"acknowledged": True},
        )

    async with create_mcp_client_session(
        url=LOGIN_FLOW_MCP_URL,
        token=login_flow_oauth_token,
        client_name="Login Flow MCP",
        elicitation_callback=elicitation_callback,
    ) as session:
        # Step 1: Provision access via Login Flow v2
        logger.info("Starting Login Flow v2 provisioning...")
        provision_result = await session.call_tool(
            "nc_auth_provision_access",
            {"scopes": None},  # Request all scopes
        )

        provision_data = json.loads(provision_result.content[0].text)
        logger.info("Provision result: %s", provision_data.get("status"))

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
            logger.info("Status check %s/%s: %s", attempt + 1, max_attempts, status)

            if status == "provisioned":
                logger.info(
                    "Login Flow v2 provisioned! Username: %s",
                    status_data.get("username"),
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


# ---------------------------------------------------------------------------
# Scope-filtered OAuth client fixtures for scope authorization tests
# These obtain tokens with specific scope subsets via the login-flow server
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
async def login_flow_read_only_token(
    anyio_backend,
    browser,
    login_flow_oauth_client_credentials,
    oauth_callback_server,
) -> str:
    """OAuth token with read-only scopes for the login-flow MCP server."""
    return await _get_oauth_token_with_scopes(
        browser,
        login_flow_oauth_client_credentials,
        oauth_callback_server,
        scopes=DEFAULT_READ_SCOPES,
        mcp_server_base_url=LOGIN_FLOW_MCP_BASE_URL,
    )


@pytest.fixture(scope="session")
async def login_flow_write_only_token(
    anyio_backend,
    browser,
    login_flow_oauth_client_credentials,
    oauth_callback_server,
) -> str:
    """OAuth token with write-only scopes for the login-flow MCP server."""
    return await _get_oauth_token_with_scopes(
        browser,
        login_flow_oauth_client_credentials,
        oauth_callback_server,
        scopes=DEFAULT_WRITE_SCOPES,
        mcp_server_base_url=LOGIN_FLOW_MCP_BASE_URL,
    )


@pytest.fixture(scope="session")
async def login_flow_full_access_token(
    anyio_backend,
    browser,
    login_flow_oauth_client_credentials,
    oauth_callback_server,
) -> str:
    """OAuth token with full access scopes for the login-flow MCP server."""
    return await _get_oauth_token_with_scopes(
        browser,
        login_flow_oauth_client_credentials,
        oauth_callback_server,
        scopes=DEFAULT_FULL_SCOPES,
        mcp_server_base_url=LOGIN_FLOW_MCP_BASE_URL,
    )


@pytest.fixture(scope="session")
async def login_flow_no_custom_scopes_token(
    anyio_backend,
    browser,
    login_flow_oauth_client_credentials,
    oauth_callback_server,
) -> str:
    """OAuth token with no custom scopes (only OIDC defaults) for the login-flow MCP server."""
    return await _get_oauth_token_with_scopes(
        browser,
        login_flow_oauth_client_credentials,
        oauth_callback_server,
        scopes="openid profile email",
        mcp_server_base_url=LOGIN_FLOW_MCP_BASE_URL,
    )


@pytest.fixture(scope="session")
async def nc_mcp_login_flow_client_read_only(
    anyio_backend, login_flow_read_only_token: str
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client with read-only scopes on the login-flow server."""
    async with create_mcp_client_session(
        url=LOGIN_FLOW_MCP_URL,
        token=login_flow_read_only_token,
        client_name="Login Flow MCP Read-Only",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def nc_mcp_login_flow_client_write_only(
    anyio_backend, login_flow_write_only_token: str
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client with write-only scopes on the login-flow server."""
    async with create_mcp_client_session(
        url=LOGIN_FLOW_MCP_URL,
        token=login_flow_write_only_token,
        client_name="Login Flow MCP Write-Only",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def nc_mcp_login_flow_client_full_access(
    anyio_backend, login_flow_full_access_token: str
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client with full access scopes on the login-flow server."""
    async with create_mcp_client_session(
        url=LOGIN_FLOW_MCP_URL,
        token=login_flow_full_access_token,
        client_name="Login Flow MCP Full Access",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def nc_mcp_login_flow_client_no_custom_scopes(
    anyio_backend, login_flow_no_custom_scopes_token: str
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client with no custom scopes on the login-flow server."""
    async with create_mcp_client_session(
        url=LOGIN_FLOW_MCP_URL,
        token=login_flow_no_custom_scopes_token,
        client_name="Login Flow MCP No Custom Scopes",
    ) as session:
        yield session


# ---------------------------------------------------------------------------
# Multi-user Login Flow fixtures for permission / isolation tests
# ---------------------------------------------------------------------------


async def _get_login_flow_token_for_user(
    browser,
    login_flow_oauth_client_credentials,
    auth_states: dict,
    username: str,
    password: str,
) -> str:
    """Get an OAuth token for a specific user targeting the login-flow MCP server.

    Similar to the global ``_get_oauth_token_for_user`` but hard-wires the
    resource / PRM discovery against port 8004.
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Login Flow tests require NEXTCLOUD_HOST")

    client_id, client_secret, callback_url, token_endpoint, authorization_endpoint = (
        login_flow_oauth_client_credentials
    )

    # Discover resource identifier from the login-flow server
    try:
        resource_metadata = await get_mcp_server_resource_metadata(
            LOGIN_FLOW_MCP_BASE_URL
        )
        resource_id = resource_metadata.get("resource")
    except Exception:
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

        # Login
        if "/login" in current_url or "/index.php/login" in current_url:
            await page.wait_for_selector('input[name="user"]', timeout=10000)
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=60000)

        # Wait for OIDC redirect chain to settle
        settle_start = time.time()
        while time.time() - settle_start < 15:
            current_url = page.url
            if "/consent" in current_url or "localhost:8081" in current_url:
                break
            await anyio.sleep(0.5)

        # Handle consent screen
        if "/consent" in page.url:
            await page.wait_for_load_state("networkidle", timeout=10000)
            await _handle_oauth_consent_screen(page, username)

        # Wait for callback
        start_time = time.time()
        while state not in auth_states:
            if time.time() - start_time > 30:
                screenshot_path = f"/tmp/login_flow_oauth_timeout_{username}.png"
                await page.screenshot(path=screenshot_path)
                raise TimeoutError(f"Timeout waiting for OAuth callback for {username}")
            await anyio.sleep(0.5)

        auth_code = auth_states[state]
    finally:
        await context.close()

    # Exchange code for token
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
        return token_response.json()["access_token"]


@pytest.fixture(scope="session")
async def all_login_flow_user_tokens(
    anyio_backend,
    browser,
    login_flow_oauth_client_credentials,
    test_users_setup,
    oauth_callback_server,
) -> dict[str, str]:
    """Fetch OAuth tokens for all test users in parallel, targeting port 8004."""
    auth_states, _ = oauth_callback_server

    start_time = time.time()
    logger.info("Fetching login-flow OAuth tokens for all users in parallel...")

    results: dict[str, str | Exception] = {}

    # Stagger per-user starts so parallel Playwright OAuth flows don't all hit
    # Nextcloud's OIDC consent rendering at once — CI under load needs a wider
    # gap (see tests/conftest.py:all_oauth_tokens).
    scale = 0.5 if "GITHUB_ACTIONS" not in os.environ else 10

    async def _fetch(username: str, config: dict, delay: float) -> None:
        if delay > 0:
            await anyio.sleep(delay)
        try:
            token = await _get_login_flow_token_for_user(
                browser,
                login_flow_oauth_client_credentials,
                auth_states,
                username,
                config["password"],
            )
            results[username] = token
        except Exception as exc:
            results[username] = exc

    user_list = list(test_users_setup.items())
    async with anyio.create_task_group() as tg:
        for idx, (username, config) in enumerate(user_list):
            tg.start_soon(_fetch, username, config, idx * scale)

    for username, result in results.items():
        if isinstance(result, Exception):
            raise result

    elapsed = time.time() - start_time
    logger.info(
        "Fetched %s login-flow tokens in %ss (~%ss per user)",
        len(results),
        format(elapsed, ".1f"),
        format(elapsed / len(results), ".1f"),
    )
    return results  # type: ignore[return-value]


async def _provision_login_flow_mcp_client(
    token: str,
    browser,
    username: str,
    password: str,
) -> AsyncGenerator[ClientSession, Any]:
    """Connect to login-flow MCP server, complete Login Flow v2 provisioning, yield session."""
    login_url_holder: dict[str, str] = {}

    async def elicitation_callback(
        context: Any,
        params: ElicitRequestParams,
    ) -> ElicitResult:
        message = params.message
        for line in message.split("\n"):
            stripped = line.strip()
            if stripped.startswith("http") and "/login/v2/" in stripped:
                login_url_holder["url"] = stripped
                break

        if "url" in login_url_holder:
            await _complete_login_flow_v2_as_user(
                browser, login_url_holder["url"], username, password
            )

        return ElicitResult(action="accept", content={"acknowledged": True})

    async with create_mcp_client_session(
        url=LOGIN_FLOW_MCP_URL,
        token=token,
        client_name=f"Login Flow MCP ({username})",
        elicitation_callback=elicitation_callback,
    ) as session:
        # Provision access
        provision_result = await session.call_tool(
            "nc_auth_provision_access", {"scopes": None}
        )
        provision_data = json.loads(provision_result.content[0].text)

        if provision_data.get("status") == "login_required":
            login_url = provision_data.get("login_url")
            if login_url and "url" not in login_url_holder:
                await _complete_login_flow_v2_as_user(
                    browser, login_url, username, password
                )

        # Poll for completion
        for attempt in range(15):
            status_result = await session.call_tool("nc_auth_check_status", {})
            status_data = json.loads(status_result.content[0].text)
            if status_data.get("status") == "provisioned":
                logger.info(
                    "Login Flow v2 provisioned for %s: %s",
                    username,
                    status_data.get("username"),
                )
                break
            if status_data.get("status") in ("not_initiated", "error"):
                raise RuntimeError(
                    f"Login Flow v2 failed for {username}: {status_data.get('message')}"
                )
            await anyio.sleep(2)
        else:
            raise TimeoutError(
                f"Login Flow v2 did not complete for {username} after 15 attempts"
            )

        yield session


async def _complete_login_flow_v2_as_user(
    browser, login_url: str, username: str, password: str
) -> None:
    """Complete Nextcloud Login Flow v2 in a browser as a specific user.

    The full Nextcloud Login Flow v2 has these steps:
    1. "Connect to your account" page -> click "Log in" button
    2. Login form -> fill username/password, submit
       (if already logged in via session cookie, this step is skipped)
    3. "Account access" grant page -> click "Grant access" button
    4. Password confirmation dialog -> enter password, click "Confirm"
    5. "Account connected" success page

    Same flow as ``_complete_login_flow_v2`` but uses the given *username* and
    *password* instead of reading from environment variables.
    """
    login_url = _rewrite_login_flow_url(login_url)

    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        logger.info("[%s] Opening Login Flow v2 URL: %s...", username, login_url[:80])
        await page.goto(login_url, wait_until="networkidle", timeout=60000)
        logger.info("[%s] Step 1 - Current URL: %s", username, page.url)

        # Step 1: "Connect to your account" page - click "Log in".
        # exact=True is required: NC33's connect page also renders an
        # "Alternative log in using app password" button, and a non-exact
        # "Log in" name substring-matches both -> Playwright strict-mode error
        # that was silently swallowed below, leaving the flow stuck on the
        # connect page (every login-flow test then times out "Login Flow v2 did
        # not complete"). NC32 has a single match, so exact=True is safe there.
        login_btn = page.get_by_role("button", name="Log in", exact=True)
        try:
            await login_btn.wait_for(timeout=10000)
            await login_btn.click()
            logger.info("[%s] Clicked 'Log in' on Connect page", username)
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            logger.info(
                "[%s] No 'Log in' button - may already be on login/grant page", username
            )

        logger.info("[%s] Step 2 - Current URL: %s", username, page.url)

        # Step 2: Login form (only if not already logged in)
        user_field = page.locator('input[name="user"]')
        if await user_field.count() > 0:
            logger.info("[%s] Login form detected, filling credentials...", username)
            await user_field.fill(username)
            await page.locator('input[name="password"]').fill(password)
            await page.get_by_role("button", name="Log in", exact=True).click()
            await page.wait_for_load_state("networkidle", timeout=60000)
            logger.info("[%s] After login: %s", username, page.url)
        else:
            logger.info("[%s] No login form - already logged in via session", username)

        # Step 3: "Account access" grant page - click "Grant access"
        grant_btn = page.get_by_role("button", name="Grant access")
        try:
            await grant_btn.wait_for(timeout=15000)
            await grant_btn.click()
            logger.info("[%s] Clicked 'Grant access'", username)
        except Exception as e:
            logger.warning("[%s] No Grant access button: %s", username, e)
            await page.screenshot(path=f"/tmp/login_flow_no_grant_{username}.png")

        # Step 4: Password confirmation dialog
        confirm_password = page.get_by_role("dialog").get_by_role(
            "textbox", name="Password"
        )
        try:
            await confirm_password.wait_for(timeout=10000)
            logger.info("[%s] Password confirmation dialog detected", username)
            await confirm_password.fill(password)
            confirm_btn = page.get_by_role("dialog").get_by_role(
                "button", name="Confirm"
            )
            await confirm_btn.wait_for(timeout=5000)
            await confirm_btn.click()
            logger.info("[%s] Clicked 'Confirm' in password dialog", username)
        except Exception:
            logger.info(
                "[%s] No password confirmation dialog (may have been auto-confirmed)",
                username,
            )

        # Step 5: Wait for "Account connected" success page
        try:
            await page.get_by_text("Account connected").wait_for(timeout=15000)
            logger.info("[%s] Login Flow v2 completed: Account connected!", username)
        except Exception:
            await page.wait_for_load_state("networkidle", timeout=10000)
            logger.info("[%s] Login Flow v2 done. Final URL: %s", username, page.url)

    finally:
        await context.close()


@pytest.fixture(scope="session")
async def alice_login_flow_mcp_client(
    anyio_backend,
    all_login_flow_user_tokens: dict[str, str],
    test_users_setup,
    browser,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated and provisioned as alice (owner role)."""
    async for session in _provision_login_flow_mcp_client(
        token=all_login_flow_user_tokens["alice"],
        browser=browser,
        username="alice",
        password=test_users_setup["alice"]["password"],
    ):
        yield session


@pytest.fixture(scope="session")
async def bob_login_flow_mcp_client(
    anyio_backend,
    all_login_flow_user_tokens: dict[str, str],
    test_users_setup,
    browser,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated and provisioned as bob (viewer role)."""
    async for session in _provision_login_flow_mcp_client(
        token=all_login_flow_user_tokens["bob"],
        browser=browser,
        username="bob",
        password=test_users_setup["bob"]["password"],
    ):
        yield session


@pytest.fixture(scope="session")
async def charlie_login_flow_mcp_client(
    anyio_backend,
    all_login_flow_user_tokens: dict[str, str],
    test_users_setup,
    browser,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated and provisioned as charlie (editor role)."""
    async for session in _provision_login_flow_mcp_client(
        token=all_login_flow_user_tokens["charlie"],
        browser=browser,
        username="charlie",
        password=test_users_setup["charlie"]["password"],
    ):
        yield session


@pytest.fixture(scope="session")
async def diana_login_flow_mcp_client(
    anyio_backend,
    all_login_flow_user_tokens: dict[str, str],
    test_users_setup,
    browser,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated and provisioned as diana (no-access role)."""
    async for session in _provision_login_flow_mcp_client(
        token=all_login_flow_user_tokens["diana"],
        browser=browser,
        username="diana",
        password=test_users_setup["diana"]["password"],
    ):
        yield session


# Static OIDC client used by the management API integration tests.
# Matches the `mcp-login-flow` allowlist
# (`ALLOWED_MGMT_CLIENT=astrolabeMcpClientOAuth00000000000`) — i.e. the same
# id `app-hooks/before-starting/26-configure-astrolabe-oauth.sh` provisions
# in real deployments — so tokens issued to it pass the management API
# allowlist check on `mcp-login-flow` and exercise the production-shaped
# code path.
STATIC_MGMT_CLIENT_ID = "astrolabeMcpClientOAuth00000000000"


@pytest.fixture(scope="session")
async def login_flow_static_client_credentials(anyio_backend, oauth_callback_server):
    """Pre-create the static OIDC client `astrolabeMcpClientOAuth00000000000`
    via `occ oidc:create` with the test's OAuth callback URL.

    The static client_id matches the id provisioned by
    `app-hooks/before-starting/26-configure-astrolabe-oauth.sh` in real
    deployments, and is allowlisted on `mcp-login-flow` via
    `ALLOWED_MGMT_CLIENT`, so tokens it issues pass the management API
    allowlist check. Uses a confidential JWT-token client to match
    production Astrolabe configuration.

    Yields: (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
    import json
    import subprocess

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Static client tests require NEXTCLOUD_HOST")

    auth_states, callback_url = oauth_callback_server
    client_id = STATIC_MGMT_CLIENT_ID

    # Idempotent: remove if a previous session left one behind
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "php",
            "/var/www/html/occ",
            "oidc:remove",
            client_id,
        ],
        check=False,
        capture_output=True,
    )

    logger.info(
        "Creating static OIDC client %s with callback %s", client_id, callback_url
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "php",
            "/var/www/html/occ",
            "oidc:create",
            "Login Flow Static Client (test)",
            callback_url,
            "--client_id",
            client_id,
            "--type",
            "confidential",
            "--flow",
            "code",
            "--token_type",
            "jwt",
            "--resource_url",
            LOGIN_FLOW_MCP_BASE_URL,
            "--allowed_scopes",
            DEFAULT_FULL_SCOPES,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        client_output = json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"occ oidc:create returned non-JSON output: {result.stdout[:200]!r}"
        ) from e
    client_secret = client_output.get("client_secret")
    if not client_secret:
        raise ValueError("occ oidc:create did not return client_secret in JSON output")

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        discovery_response = await http_client.get(
            f"{nextcloud_host}/.well-known/openid-configuration"
        )
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

    yield (
        client_id,
        client_secret,
        callback_url,
        oidc_config["token_endpoint"],
        oidc_config["authorization_endpoint"],
    )

    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "php",
            "/var/www/html/occ",
            "oidc:remove",
            client_id,
        ],
        check=False,
        capture_output=True,
    )


@pytest.fixture(scope="session")
async def login_flow_static_client_token(
    anyio_backend,
    browser,
    login_flow_static_client_credentials,
    oauth_callback_server,
) -> str:
    """Drive the OAuth auth-code flow using the static OIDC client and
    return the raw access_token string.

    Mirrors `login_flow_oauth_token` but feeds it static credentials instead
    of a DCR-generated client. Required for hitting management API
    endpoints which gate on `ALLOWED_MGMT_CLIENT`.
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")
    password = os.getenv("NEXTCLOUD_PASSWORD")
    if not all([nextcloud_host, username, password]):
        pytest.skip(
            "Static client OAuth requires NEXTCLOUD_HOST, NEXTCLOUD_USERNAME, NEXTCLOUD_PASSWORD"
        )

    auth_states, _ = oauth_callback_server
    client_id, client_secret, callback_url, token_endpoint, authorization_endpoint = (
        login_flow_static_client_credentials
    )

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

    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()
    try:
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)
        if "/login" in page.url or "/index.php/login" in page.url:
            await page.wait_for_selector('input[name="user"]', timeout=10000)
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=60000)

        # After login the oidc app issues a JS-driven re-authorize chain
        # (/apps/oidc/redirect → /apps/oidc/authorize → /apps/oidc/consent).
        # networkidle can fire during the gap before consent renders, so
        # poll for either the consent page or the callback hit before
        # bailing.
        start = time.time()
        consent_handled = False
        while state not in auth_states:
            if time.time() - start > 60:
                raise TimeoutError("Timeout waiting for OAuth callback")
            if not consent_handled:
                try:
                    handled = await _handle_oauth_consent_screen(page, username)
                    if handled:
                        consent_handled = True
                except Exception as e:
                    logger.warning("Consent screen handling raised: %s", e)
                    consent_handled = True  # don't retry indefinitely
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

    return token_data["access_token"]
