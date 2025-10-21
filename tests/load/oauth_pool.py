"""
OAuth User Pool Management for Load Testing.

Manages multiple OAuth-authenticated users for realistic multi-user load testing scenarios.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


@dataclass
class UserConfig:
    """Configuration for a single test user."""

    username: str
    password: str
    display_name: str
    email: str
    groups: list[str]


@dataclass
class UserProfile:
    """Profile for an OAuth-authenticated user."""

    username: str
    password: str
    token: str
    session: ClientSession | None = None
    streamable_context: Any | None = None  # Store for proper cleanup
    operation_count: int = 0
    error_count: int = 0


class OAuthUserPool:
    """
    Manages a pool of OAuth-authenticated users for load testing.

    Handles token acquisition, session management, and user lifecycle.
    """

    def __init__(
        self,
        admin_client: Any,  # NextcloudClient with admin credentials
        client_id: str,
        client_secret: str,
        callback_url: str,
        token_endpoint: str,
        authorization_endpoint: str,
    ):
        self.admin_client = admin_client  # For user management
        self.nextcloud_host = str(admin_client._client.base_url)
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_url = callback_url
        self.token_endpoint = token_endpoint
        self.authorization_endpoint = authorization_endpoint
        self.users: dict[str, UserProfile] = {}
        self._http_client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        """Initialize HTTP client."""
        self._http_client = httpx.AsyncClient(verify=False, timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Cleanup HTTP client."""
        if self._http_client:
            await self._http_client.aclose()

    async def acquire_token(self, username: str, password: str, auth_code: str) -> str:
        """
        Exchange authorization code for OAuth access token.

        Args:
            username: Username for logging
            password: Password (for logging/debugging)
            auth_code: Authorization code from OAuth flow

        Returns:
            OAuth access token
        """
        logger.info(f"Exchanging auth code for access token (user: {username})...")

        if not self._http_client:
            raise RuntimeError(
                "HTTP client not initialized - use async context manager"
            )

        # Exchange authorization code for access token
        token_response = await self._http_client.post(
            self.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": self.callback_url,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_response.raise_for_status()
        token_data = token_response.json()

        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError(f"No access token in response for {username}")

        logger.info(f"Successfully acquired OAuth token for {username}")
        return access_token

    async def add_user(self, username: str, password: str, token: str) -> UserProfile:
        """
        Add a user to the pool with their OAuth token.

        Args:
            username: Username
            password: Password (for future re-auth if needed)
            token: OAuth access token

        Returns:
            UserProfile for the added user
        """
        if username in self.users:
            logger.warning(f"User {username} already in pool, updating token")

        profile = UserProfile(username=username, password=password, token=token)
        self.users[username] = profile
        logger.info(f"Added user {username} to pool (total: {len(self.users)})")
        return profile

    async def create_user_session(
        self, username: str, mcp_url: str = "http://127.0.0.1:8001/mcp"
    ) -> ClientSession:
        """
        Create an MCP client session for a user.

        Args:
            username: Username to create session for
            mcp_url: MCP server URL

        Returns:
            Initialized ClientSession

        Raises:
            KeyError: If user not in pool
        """
        if username not in self.users:
            raise KeyError(f"User {username} not in pool")

        profile = self.users[username]

        # Create streamable HTTP connection with OAuth token in Authorization header
        # This matches the pattern from tests/conftest.py create_mcp_client_session()
        headers = {"Authorization": f"Bearer {profile.token}"}
        streamable_context = streamablehttp_client(mcp_url, headers=headers)

        try:
            read_stream, write_stream, _ = await streamable_context.__aenter__()

            session = ClientSession(read_stream, write_stream)
            await session.__aenter__()
            await session.initialize()

            # Store both session and context for proper cleanup
            profile.session = session
            profile.streamable_context = streamable_context
            logger.info(f"Created MCP session for {username}")
            return session

        except Exception as e:
            # Clean up streamable context if session creation failed
            try:
                await streamable_context.__aexit__(None, None, None)
            except Exception as cleanup_error:
                logger.debug(f"Error during cleanup: {cleanup_error}")
            raise e

    async def close_user_session(self, username: str):
        """Close the MCP session for a user."""
        if username not in self.users:
            return

        profile = self.users[username]

        # Close ClientSession
        if profile.session:
            try:
                await profile.session.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"Error closing session for {username}: {e}")
            profile.session = None

        # Close streamable context
        if profile.streamable_context:
            try:
                await profile.streamable_context.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"Error closing streamable context for {username}: {e}")
            profile.streamable_context = None

    async def close_all_sessions(self):
        """Close all user sessions."""
        for username in list(self.users.keys()):
            await self.close_user_session(username)

    def get_user(self, username: str) -> UserProfile:
        """Get user profile by username."""
        if username not in self.users:
            raise KeyError(f"User {username} not in pool")
        return self.users[username]

    def get_all_users(self) -> list[UserProfile]:
        """Get all user profiles."""
        return list(self.users.values())

    def record_operation(self, username: str, success: bool = True):
        """Record an operation for user stats."""
        if username in self.users:
            self.users[username].operation_count += 1
            if not success:
                self.users[username].error_count += 1

    def get_stats(self) -> dict[str, dict[str, int | float]]:
        """Get per-user operation statistics."""
        return {
            username: {
                "operations": profile.operation_count,
                "errors": profile.error_count,
                "success_rate": (
                    (profile.operation_count - profile.error_count)
                    / max(profile.operation_count, 1)
                    * 100
                ),
            }
            for username, profile in self.users.items()
        }

    async def create_nextcloud_user(
        self,
        username: str,
        password: str,
        display_name: str | None = None,
        email: str | None = None,
    ) -> UserConfig:
        """
        Create a Nextcloud user via the Users API.

        Args:
            username: Username for the new user
            password: Password for the new user
            display_name: Optional display name
            email: Optional email address

        Returns:
            UserConfig for the created user

        Raises:
            HTTPStatusError: If user creation fails
        """
        logger.info(f"Creating Nextcloud user: {username}")

        await self.admin_client.users.create_user(
            userid=username,
            password=password,
            display_name=display_name or username,
            email=email or f"{username}@benchmark.local",
        )

        logger.info(f"Successfully created Nextcloud user: {username}")

        return UserConfig(
            username=username,
            password=password,
            display_name=display_name or username,
            email=email or f"{username}@benchmark.local",
            groups=[],
        )

    async def delete_nextcloud_user(self, username: str):
        """
        Delete a Nextcloud user via the Users API.

        Args:
            username: Username to delete
        """
        logger.info(f"Deleting Nextcloud user: {username}")

        try:
            await self.admin_client.users.delete_user(userid=username)
            logger.info(f"Successfully deleted Nextcloud user: {username}")
        except Exception as e:
            logger.warning(f"Failed to delete user {username}: {e}")

    async def acquire_token_playwright(
        self,
        browser: Any,
        username: str,
        password: str,
        state: str,
        auth_states: dict[str, str],
    ) -> str:
        """
        Acquire OAuth token via Playwright browser automation.

        Based on conftest.py playwright_oauth_token fixture.
        Automates the full OAuth flow:
        1. Navigate to authorization URL
        2. Fill login form
        3. Handle OAuth consent
        4. Wait for callback server to receive auth code
        5. Exchange code for access token

        Args:
            browser: Playwright browser instance
            username: Username to authenticate
            password: Password for the user
            state: Unique state parameter for this OAuth flow
            auth_states: Dict mapping state -> auth_code (shared with callback server)

        Returns:
            OAuth access token

        Raises:
            TimeoutError: If callback not received within timeout
            ValueError: If token exchange fails
        """
        import time
        from urllib.parse import quote

        logger.info(f"Starting Playwright OAuth flow for {username}...")
        logger.debug(f"Using state: {state[:16]}...")

        # Construct authorization URL
        auth_url = (
            f"{self.authorization_endpoint}?"
            f"response_type=code&"
            f"client_id={self.client_id}&"
            f"redirect_uri={quote(self.callback_url, safe='')}&"
            f"state={state}&"
            f"scope=openid%20profile%20email"
        )

        # Browser automation
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        try:
            # Navigate to authorization URL
            logger.debug("Navigating to authorization URL...")
            await page.goto(auth_url, wait_until="networkidle", timeout=30000)
            current_url = page.url

            # Login if needed
            if "/login" in current_url or "/index.php/login" in current_url:
                logger.info(f"Logging in as {username}...")
                await page.wait_for_selector('input[name="user"]', timeout=10000)
                await page.fill('input[name="user"]', username)
                await page.fill('input[name="password"]', password)
                await page.click('button[type="submit"]')
                await page.wait_for_load_state("networkidle", timeout=30000)
                current_url = page.url
                logger.info("Login completed")

            # Handle OAuth consent if present
            try:
                authorize_button = await page.query_selector(
                    'button:has-text("Authorize"), button:has-text("Allow"), input[type="submit"][value*="uthoriz"]'
                )
                if authorize_button:
                    logger.info("Authorizing OAuth client...")
                    await authorize_button.click()
                    await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as e:
                logger.debug(f"No authorization needed: {e}")

            # Wait for callback server to receive auth code
            logger.info("Waiting for OAuth callback...")
            timeout_seconds = 30
            start_time = time.time()
            while state not in auth_states:
                if time.time() - start_time > timeout_seconds:
                    screenshot_path = f"/tmp/oauth_timeout_{username}.png"
                    await page.screenshot(path=screenshot_path)
                    logger.error(f"Screenshot saved to {screenshot_path}")
                    raise TimeoutError(
                        f"Timeout waiting for OAuth callback for {username}"
                    )
                await asyncio.sleep(0.5)

            auth_code = auth_states[state]
            logger.info(f"Received auth code for {username}")

        finally:
            await context.close()

        # Exchange code for token
        logger.info(f"Exchanging auth code for access token ({username})...")
        token_response = await self._http_client.post(
            self.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": self.callback_url,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_response.raise_for_status()
        token_data = token_response.json()

        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError(f"No access token for {username}: {token_data}")

        logger.info(f"Successfully acquired OAuth token for {username}")
        return access_token


class UserSessionWrapper:
    """
    Wrapper for a user-specific MCP session with operation tracking.

    Provides a convenient interface for executing operations as a specific user.
    """

    def __init__(self, username: str, session: ClientSession, pool: OAuthUserPool):
        self.username = username
        self.session = session
        self.pool = pool

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Call an MCP tool and record the operation.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool result
        """
        try:
            result = await self.session.call_tool(tool_name, arguments)
            self.pool.record_operation(self.username, success=True)
            return result
        except Exception:
            self.pool.record_operation(self.username, success=False)
            raise

    async def read_resource(self, uri: str) -> Any:
        """
        Read an MCP resource and record the operation.

        Args:
            uri: Resource URI

        Returns:
            Resource data
        """
        try:
            result = await self.session.read_resource(uri)
            self.pool.record_operation(self.username, success=True)
            return result
        except Exception:
            self.pool.record_operation(self.username, success=False)
            raise


def generate_secure_password(length: int = 20) -> str:
    """Generate a secure random password."""
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()"
    return "".join(secrets.choice(alphabet) for _ in range(length))
