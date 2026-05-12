import base64
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, AsyncGenerator, AsyncIterator
from urllib.parse import parse_qs, quote, urlparse

import anyio
import httpx
import pytest
from httpx import HTTPStatusError
from mcp import ClientSession
from mcp.client.session import RequestContext
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import ElicitRequestParams, ElicitResult, ErrorData
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# Default scopes for OAuth testing - all app-specific read/write scopes
DEFAULT_FULL_SCOPES = (
    "openid profile email "
    "notes.read notes.write "
    "calendar.read calendar.write "
    "todo.read todo.write "
    "contacts.read contacts.write "
    "cookbook.read cookbook.write "
    "deck.read deck.write "
    "tables.read tables.write "
    "files.read files.write "
    "sharing.read sharing.write "
    "talk.read talk.write"
)

# Read-only scopes (all read scopes across apps) - should match DEFAULT_FULL_SCOPES read portion
DEFAULT_READ_SCOPES = (
    "openid profile email "
    "notes.read "
    "calendar.read "
    "todo.read "
    "contacts.read "
    "cookbook.read "
    "deck.read "
    "tables.read "
    "files.read "
    "sharing.read "
    "talk.read"
)

# Write-only scopes (all write scopes across apps) - should match DEFAULT_FULL_SCOPES write portion
DEFAULT_WRITE_SCOPES = (
    "openid profile email "
    "notes.write "
    "calendar.write "
    "todo.write "
    "contacts.write "
    "cookbook.write "
    "deck.write "
    "tables.write "
    "files.write "
    "sharing.write "
    "talk.write"
)


@pytest.fixture(scope="session")
def anyio_backend():
    """Configure anyio to use asyncio backend for all tests."""
    return "asyncio"


async def wait_for_nextcloud(
    host: str, max_attempts: int = 30, delay: float = 2.0
) -> bool:
    """
    Wait for Nextcloud server to be ready by checking the status endpoint.

    Args:
        host: Nextcloud host URL
        max_attempts: Maximum number of connection attempts
        delay: Delay between attempts in seconds

    Returns:
        True if server is ready, False otherwise
    """
    logger.info("Waiting for Nextcloud server at %s to be ready...", host)

    async with httpx.AsyncClient(timeout=5.0) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                # Try to hit the status endpoint
                response = await client.get(f"{host}/status.php")
                if response.status_code == 200:
                    data = response.json()
                    if data.get("installed"):
                        logger.info(
                            "Nextcloud server is ready (version: %s)",
                            data.get("versionstring", "unknown"),
                        )
                        return True
            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.debug("Attempt %s/%s: %s", attempt, max_attempts, e)

            if attempt < max_attempts:
                logger.info(
                    "Nextcloud not ready yet, waiting %ss... (attempt %s/%s)",
                    delay,
                    attempt,
                    max_attempts,
                )
                await anyio.sleep(delay)

    logger.error(
        "Nextcloud server at %s did not become ready after %s attempts",
        host,
        max_attempts,
    )
    return False


@asynccontextmanager
async def create_mcp_client_session(
    url: str,
    token: str | None = None,
    client_name: str = "MCP",
    elicitation_callback: Any = None,
    sampling_callback: Any = None,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[ClientSession]:
    """
    Factory function to create an MCP client session with proper lifecycle management.

    Uses native async context managers to ensure correct LIFO cleanup order,
    eliminating the need for exception suppression. Python's context manager protocol
    guarantees that cleanup happens in reverse order of entry.

    Consolidates the common pattern used by all MCP client fixtures:
    - Creates streamable HTTP client with optional OAuth token
    - Initializes MCP ClientSession
    - Ensures proper cleanup without suppressing errors

    Args:
        url: MCP server URL (e.g., "http://localhost:8000/mcp")
        token: Optional OAuth access token for Bearer authentication
        client_name: Client name for logging (e.g., "OAuth MCP (Playwright)")
        elicitation_callback: Optional callback for handling elicitation requests.
            Should match signature: async def callback(context: RequestContext, params: ElicitRequestParams) -> ElicitResult | ErrorData
        sampling_callback: Optional callback for handling sampling (LLM generation) requests.
            Should match signature: async def callback(context: RequestContext, params: CreateMessageRequestParams) -> CreateMessageResult | ErrorData
        headers: Optional custom headers (e.g., for BasicAuth). If both headers and token are provided,
            custom headers take precedence.

    Yields:
        Initialized MCP ClientSession

    Note:
        This implementation uses native async context managers instead of manually
        calling __aenter__/__aexit__. This ensures that anyio's structured concurrency
        requirements are met, as Python guarantees LIFO cleanup order for nested
        context managers. See: https://github.com/modelcontextprotocol/python-sdk/issues/577
    """
    logger.info("Creating Streamable HTTP client for %s", client_name)

    # Prepare headers - custom headers take precedence over token-based auth
    if headers is None:
        headers = {"Authorization": f"Bearer {token}"} if token else None

    # Use native async with - Python ensures LIFO cleanup
    # Cleanup order will be: ClientSession.__aexit__ -> streamablehttp_client.__aexit__
    async with streamablehttp_client(url, headers=headers) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(
            read_stream,
            write_stream,
            elicitation_callback=elicitation_callback,
            sampling_callback=sampling_callback,
        ) as session:
            await session.initialize()
            logger.info("%s client session initialized successfully", client_name)
            yield session

    # Cleanup happens automatically in LIFO order - no exception suppression needed
    logger.debug("%s client session cleaned up successfully", client_name)


@pytest.fixture(scope="session")
async def nc_client(anyio_backend) -> AsyncGenerator[NextcloudClient, Any]:
    """
    Fixture to create a NextcloudClient instance for integration tests.
    Uses environment variables for configuration.
    Waits for Nextcloud to be ready before proceeding.
    """

    assert os.getenv("NEXTCLOUD_HOST"), "NEXTCLOUD_HOST env var not set"
    assert os.getenv("NEXTCLOUD_USERNAME"), "NEXTCLOUD_USERNAME env var not set"
    assert os.getenv("NEXTCLOUD_PASSWORD"), "NEXTCLOUD_PASSWORD env var not set"

    host = os.getenv("NEXTCLOUD_HOST")

    # Wait for Nextcloud to be ready
    if not await wait_for_nextcloud(host):
        pytest.fail(f"Nextcloud server at {host} is not ready")

    logger.info("Creating session-scoped NextcloudClient from environment variables.")
    client = NextcloudClient.from_env()

    # Perform a quick check to ensure connection works
    try:
        await client.capabilities()
        logger.info(
            "NextcloudClient session fixture initialized and capabilities checked."
        )
        yield client
    except Exception as e:
        logger.error("Failed to initialize NextcloudClient session fixture: %s", e)
        pytest.fail(f"Failed to connect to Nextcloud or get capabilities: {e}")
    finally:
        await client.close()


@pytest.fixture(scope="session")
async def nc_mcp_client(anyio_backend) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session for integration tests using streamable-http.

    Uses anyio pytest plugin for proper async fixture handling.
    """
    async with create_mcp_client_session(
        url="http://localhost:8000/mcp",
        client_name="Basic MCP (HTTP)",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def nc_mcp_oauth_client(
    anyio_backend,
    playwright_oauth_token: str,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session for OAuth integration tests using Playwright automation.
    Connects to the OAuth-enabled MCP server on port 8001 with OAuth authentication.

    Uses headless browser automation suitable for CI/CD.
    Uses anyio pytest plugin for proper async fixture handling.
    """
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token,
        client_name="OAuth MCP (Playwright)",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def nc_mcp_basic_auth_client(
    anyio_backend,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session with BasicAuth credentials.
    Connects to the multi-user BasicAuth MCP server on port 8003 with MCP_DEPLOYMENT_MODE=multi_user_basic.

    Uses BasicAuth credentials for multi-user pass-through mode (ADR-020).
    Credentials are passed in Authorization header and forwarded to Nextcloud APIs.

    Uses anyio pytest plugin for proper async fixture handling.
    """

    credentials = base64.b64encode(b"admin:admin").decode("utf-8")
    auth_header = f"Basic {credentials}"

    async with create_mcp_client_session(
        url="http://localhost:8003/mcp",
        headers={"Authorization": auth_header},
        client_name="BasicAuth MCP (Multi-User)",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def nc_mcp_oauth_jwt_client(
    anyio_backend,
    playwright_oauth_token_jwt: str,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session for JWT OAuth integration tests.
    Connects to the OAuth-enabled MCP server on port 8001 with JWT token authentication.

    Uses JWT tokens (RFC 9068) which provide:
    - Token validation via JWT signature verification (JWKS)
    - Scope information embedded in token claims
    - Faster validation without userinfo endpoint call

    Uses headless browser automation suitable for CI/CD.
    Uses anyio pytest plugin for proper async fixture handling.
    """
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token_jwt,
        client_name="OAuth JWT MCP (Playwright)",
    ) as session:
        yield session


@pytest.fixture
async def nc_mcp_oauth_client_with_elicitation(
    anyio_backend,
    playwright_oauth_token: str,
    browser,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session with elicitation callback support.

    This fixture enables REAL elicitation testing by providing a callback that:
    1. Extracts OAuth URL from elicitation message
    2. Uses Playwright to complete OAuth flow automatically
    3. Returns acceptance to confirm completion

    This allows testing the complete login elicitation flow (ADR-006) end-to-end,
    verifying that:
    - The check_logged_in tool triggers elicitation for unauthenticated users
    - The OAuth flow completes successfully via automated browser
    - Refresh token is stored after OAuth completion
    - The tool returns "yes" after successful login

    Uses function scope to allow each test to have independent elicitation state.
    """
    # Get credentials from environment
    username = os.getenv("NEXTCLOUD_USERNAME")
    password = os.getenv("NEXTCLOUD_PASSWORD")

    if not all([username, password]):
        pytest.skip(
            "Elicitation test requires NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD"
        )

    # Track whether elicitation was triggered (for test validation)
    elicitation_triggered = {"count": 0}

    async def elicitation_callback(
        context: RequestContext[ClientSession, Any],
        params: ElicitRequestParams,
    ) -> ElicitResult | ErrorData:
        """Handle elicitation by completing OAuth flow with Playwright."""
        elicitation_triggered["count"] += 1

        logger.info("🎯 Elicitation callback invoked!")
        logger.info("  Message: %s...", params.message[:100])
        logger.info("  Schema: %s", params.schema)

        # Extract OAuth URL from elicitation message

        url_pattern = r"https?://[^\s]+"
        urls = re.findall(url_pattern, params.message)

        if not urls:
            error_msg = "No URL found in elicitation message"
            logger.error("❌ %s", error_msg)
            return ErrorData(code=-32602, message=error_msg)

        oauth_url = urls[0]
        logger.info("  Extracted URL: %s", oauth_url)

        # Complete OAuth flow with Playwright
        page = await browser.new_page()
        try:
            logger.info("🌐 Navigating to OAuth URL...")
            await page.goto(oauth_url, timeout=60000)

            current_url = page.url
            logger.info("  Current URL after navigation: %s", current_url)

            # Handle login form if present
            if "/login" in current_url or "/index.php/login" in current_url:
                logger.info("🔐 Login page detected, filling credentials...")
                await page.wait_for_selector('input[name="user"]', timeout=10000)
                await page.fill('input[name="user"]', username)
                await page.fill('input[name="password"]', password)
                await page.click('button[type="submit"]')
                await page.wait_for_load_state("networkidle", timeout=60000)
                logger.info("  ✓ Login completed")

            # Wait for the OIDC redirect chain to settle before handling consent.
            logger.info("  Waiting for OIDC redirect chain to settle...")
            settle_start = time.time()
            while time.time() - settle_start < 15:
                current_url = page.url
                if "/consent" in current_url or "/callback" in current_url:
                    break
                await anyio.sleep(0.5)

            # Handle consent screen if present
            if "/consent" in page.url:
                await page.wait_for_load_state("networkidle", timeout=10000)
                try:
                    logger.info("  Current URL before consent: %s", page.url)
                    consent_handled = await _handle_oauth_consent_screen(page, username)
                    if consent_handled:
                        logger.info("  ✓ Consent granted")
                    else:
                        logger.warning("  ⚠ Consent handler returned False")
                except Exception as e:
                    logger.warning("  ⚠ Consent screen handling failed: %s", e)
                    screenshot_path = (
                        f"/tmp/elicitation_consent_error_{uuid.uuid4()}.png"
                    )
                    await page.screenshot(path=screenshot_path)
                    logger.info("  Screenshot saved: %s", screenshot_path)
            else:
                logger.debug("  No consent screen (URL: %s)", page.url)

            # Wait for OAuth callback URL to be reached
            # The MCP server's callback endpoint will handle token exchange
            logger.info("⏳ Waiting for OAuth callback to complete...")

            # Wait for URL to contain /oauth/callback or a success page
            # Give it up to 30 seconds for the redirect and token exchange
            for _ in range(60):  # 60 * 0.5s = 30s max wait
                await anyio.sleep(0.5)
                current_url = page.url
                if "/oauth/callback" in current_url or "/user" in current_url:
                    logger.info("  ✓ Callback URL reached: %s", current_url)
                    break
            else:
                logger.warning(
                    "  ⚠ Timeout waiting for callback, final URL: %s", page.url
                )

            # Wait a bit more to ensure the server processed the callback
            await anyio.sleep(2)

            final_url = page.url
            logger.info("  Final URL: %s", final_url)

            # Return success - user "accepted" the elicitation
            logger.info("✅ OAuth flow completed, returning accept")
            return ElicitResult(action="accept", content={"acknowledged": True})

        except Exception as e:
            logger.error("❌ Elicitation OAuth flow failed: %s", e)
            # Take screenshot for debugging
            try:
                screenshot_path = f"/tmp/elicitation_oauth_failure_{uuid.uuid4()}.png"
                await page.screenshot(path=screenshot_path)
                logger.error("  Screenshot saved: %s", screenshot_path)
            except Exception:
                pass

            return ErrorData(
                code=-32603, message=f"Failed to complete OAuth flow: {str(e)}"
            )

        finally:
            await page.close()

    # Create client session with elicitation callback
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token,
        client_name="OAuth MCP with Elicitation",
        elicitation_callback=elicitation_callback,
    ) as session:
        # Attach elicitation metadata for test validation
        session.elicitation_triggered = elicitation_triggered
        yield session


@pytest.fixture(scope="session")
async def nc_mcp_oauth_client_read_only(
    anyio_backend,
    playwright_oauth_token_read_only: str,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session with only read scopes.
    Connects to the OAuth-enabled MCP server on port 8001.

    This client should only see read tools and should get 403 errors
    when attempting to call write tools.

    Uses JWT tokens because they embed scope information in claims,
    enabling proper scope-based tool filtering.
    """
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token_read_only,
        client_name="OAuth JWT MCP Read-Only (Playwright)",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def nc_mcp_oauth_client_write_only(
    anyio_backend,
    playwright_oauth_token_write_only: str,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session with only write scopes.
    Connects to the OAuth-enabled MCP server on port 8001.

    This client should only see write tools and should get 403 errors
    when attempting to call read tools.

    Uses JWT tokens because they embed scope information in claims,
    enabling proper scope-based tool filtering.
    """
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token_write_only,
        client_name="OAuth JWT MCP Write-Only (Playwright)",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def nc_mcp_oauth_client_full_access(
    anyio_backend,
    playwright_oauth_token_full_access: str,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session with both read and write scopes.
    Connects to the OAuth-enabled MCP server on port 8001.

    This client should see all tools and be able to call all operations.

    Uses JWT tokens because they embed scope information in claims,
    enabling proper scope-based tool filtering.
    """
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token_full_access,
        client_name="OAuth JWT MCP Full Access (Playwright)",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def nc_mcp_oauth_client_no_custom_scopes(
    anyio_backend,
    playwright_oauth_token_no_custom_scopes: str,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session with NO custom scopes.
    Connects to the OAuth-enabled MCP server on port 8001.

    This client has only OIDC default scopes (openid, profile, email) without
    application-specific scopes (notes.read, notes.write, etc.).

    Expected behavior: Should see 0 tools (all tools require custom scopes).

    Uses JWT tokens because they embed scope information in claims,
    enabling proper scope-based tool filtering.
    """
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token_no_custom_scopes,
        client_name="OAuth JWT MCP No Custom Scopes (Playwright)",
    ) as session:
        yield session


@pytest.fixture
async def temporary_note(nc_client: NextcloudClient):
    """
    Fixture to create a temporary note for a test and ensure its deletion afterward.
    Yields the created note dictionary.
    """

    note_id = None
    unique_suffix = uuid.uuid4().hex[:8]
    note_title = f"Temporary Test Note {unique_suffix}"
    note_content = f"Content for temporary note {unique_suffix}"
    note_category = "TemporaryTesting"
    created_note_data = None

    logger.info("Creating temporary note: %s", note_title)
    try:
        created_note_data = await nc_client.notes.create_note(
            title=note_title, content=note_content, category=note_category
        )
        note_id = created_note_data.get("id")
        if not note_id:
            pytest.fail("Failed to get ID from created temporary note.")

        logger.info("Temporary note created with ID: %s", note_id)
        yield created_note_data  # Provide the created note data to the test

    finally:
        if note_id:
            logger.info("Cleaning up temporary note ID: %s", note_id)
            try:
                await nc_client.notes.delete_note(note_id=note_id)
                logger.info("Successfully deleted temporary note ID: %s", note_id)
            except HTTPStatusError as e:
                # Ignore 404 if note was already deleted by the test itself
                if e.response.status_code != 404:
                    logger.error(
                        "HTTP error deleting temporary note %s: %s", note_id, e
                    )
                else:
                    logger.warning("Temporary note %s already deleted (404).", note_id)
            except Exception as e:
                logger.error(
                    "Unexpected error deleting temporary note %s: %s", note_id, e
                )


@pytest.fixture
async def temporary_note_factory(nc_client: NextcloudClient):
    """
    Factory fixture to create multiple temporary notes with custom parameters.
    Returns a callable that creates notes and tracks them for automatic cleanup.
    """
    created_notes = []

    async def _create_note(title: str, content: str, category: str = ""):
        """Create a temporary note with custom title, content, and category."""
        logger.info("Creating temporary note via factory: %s", title)
        note_data = await nc_client.notes.create_note(
            title=title, content=content, category=category
        )
        note_id = note_data.get("id")
        if note_id:
            created_notes.append(note_id)
            logger.info("Factory created note ID: %s", note_id)
        return note_data

    yield _create_note

    # Cleanup all created notes
    for note_id in created_notes:
        logger.info("Cleaning up factory-created note ID: %s", note_id)
        try:
            await nc_client.notes.delete_note(note_id=note_id)
            logger.info("Successfully deleted factory note ID: %s", note_id)
        except HTTPStatusError as e:
            if e.response.status_code != 404:
                logger.error("HTTP error deleting factory note %s: %s", note_id, e)
            else:
                logger.warning("Factory note %s already deleted (404).", note_id)
        except Exception as e:
            logger.error("Unexpected error deleting factory note %s: %s", note_id, e)


@pytest.fixture
async def temporary_note_with_attachment(
    nc_client: NextcloudClient, temporary_note: dict
):
    """
    Fixture that creates a temporary note, adds an attachment, and cleans up both.
    Yields a tuple: (note_data, attachment_filename, attachment_content).
    Depends on the temporary_note fixture.
    """

    note_data = temporary_note
    note_id = note_data["id"]
    note_category = note_data.get("category")  # Get category from the note data
    unique_suffix = uuid.uuid4().hex[:8]
    attachment_filename = f"temp_attach_{unique_suffix}.txt"
    attachment_content = f"Content for {attachment_filename}".encode("utf-8")
    attachment_mime = "text/plain"

    logger.info(
        "Adding attachment '%s' to temporary note ID: %s (category: '%s')",
        attachment_filename,
        note_id,
        note_category or "",
    )
    try:
        # Pass the category to add_note_attachment
        upload_response = await nc_client.webdav.add_note_attachment(
            note_id=note_id,
            filename=attachment_filename,
            content=attachment_content,
            category=note_category,  # Pass the fetched category
            mime_type=attachment_mime,
        )
        assert upload_response.get("status_code") in [
            201,
            204,
        ], f"Failed to upload attachment: {upload_response}"
        logger.info("Attachment '%s' added successfully.", attachment_filename)

        yield note_data, attachment_filename, attachment_content

        # Cleanup for the attachment is handled by the notes_delete_note call
        # in the temporary_note fixture's finally block (which deletes the .attachments dir)

    except Exception as e:
        logger.error("Failed to add attachment in fixture: %s", e)
        pytest.fail(f"Fixture setup failed during attachment upload: {e}")

    # Note: The temporary_note fixture's finally block will handle note deletion,
    # which should also trigger the WebDAV directory deletion attempt.


@pytest.fixture(scope="module")
async def temporary_addressbook(nc_client: NextcloudClient):
    """
    Fixture to create a temporary addressbook for a test and ensure its deletion afterward.
    Yields the created addressbook dictionary.
    """
    addressbook_name = f"test-addressbook-{uuid.uuid4().hex[:8]}"
    logger.info("Creating temporary addressbook: %s", addressbook_name)
    try:
        await nc_client.contacts.create_addressbook(
            name=addressbook_name, display_name=f"Test Addressbook {addressbook_name}"
        )
        logger.info("Temporary addressbook created: %s", addressbook_name)
        yield addressbook_name
    finally:
        logger.info("Cleaning up temporary addressbook: %s", addressbook_name)
        try:
            await nc_client.contacts.delete_addressbook(name=addressbook_name)
            logger.info(
                "Successfully deleted temporary addressbook: %s", addressbook_name
            )
        except HTTPStatusError as e:
            if e.response.status_code != 404:
                logger.error(
                    "HTTP error deleting temporary addressbook %s: %s",
                    addressbook_name,
                    e,
                )
            else:
                logger.warning(
                    "Temporary addressbook %s already deleted (404).", addressbook_name
                )
        except Exception as e:
            logger.error(
                "Unexpected error deleting temporary addressbook %s: %s",
                addressbook_name,
                e,
            )


@pytest.fixture
async def temporary_contact(nc_client: NextcloudClient, temporary_addressbook: str):
    """
    Fixture to create a temporary contact in a temporary addressbook and ensure its deletion.
    Yields the created contact's UID.
    """
    contact_uid = f"test-contact-{uuid.uuid4().hex[:8]}"
    addressbook_name = temporary_addressbook
    contact_data = {
        "fn": "John Doe",
        "email": "john.doe@example.com",
        "tel": "1234567890",
    }
    logger.info("Creating temporary contact in addressbook: %s", addressbook_name)
    try:
        await nc_client.contacts.create_contact(
            addressbook=addressbook_name,
            uid=contact_uid,
            contact_data=contact_data,
        )
        logger.info("Temporary contact created with UID: %s", contact_uid)
        yield contact_uid
    finally:
        logger.info("Cleaning up temporary contact: %s", contact_uid)
        try:
            await nc_client.contacts.delete_contact(
                addressbook=addressbook_name, uid=contact_uid
            )
            logger.info("Successfully deleted temporary contact: %s", contact_uid)
        except HTTPStatusError as e:
            if e.response.status_code != 404:
                logger.error(
                    "HTTP error deleting temporary contact %s: %s", contact_uid, e
                )
            else:
                logger.warning(
                    "Temporary contact %s already deleted (404).", contact_uid
                )
        except Exception as e:
            logger.error(
                "Unexpected error deleting temporary contact %s: %s", contact_uid, e
            )


@pytest.fixture
async def temporary_board(nc_client: NextcloudClient):
    """
    Fixture to create a temporary deck board for tests and ensure its deletion afterward.
    Yields the created board data dict.
    """
    board_id = None
    unique_suffix = uuid.uuid4().hex[:8]
    board_title = f"Temporary Test Board {unique_suffix}"
    board_color = "FF0000"  # Red color
    created_board_data = None

    logger.info("Creating temporary deck board: %s", board_title)
    try:
        created_board = await nc_client.deck.create_board(board_title, board_color)
        board_id = created_board.id
        created_board_data = {
            "id": board_id,
            "title": created_board.title,
            "color": created_board.color,
            "archived": getattr(created_board, "archived", False),
        }

        logger.info("Temporary board created with ID: %s", board_id)
        yield created_board_data

    finally:
        if board_id:
            logger.info("Cleaning up temporary board ID: %s", board_id)
            try:
                await nc_client.deck.delete_board(board_id)
                logger.info("Successfully deleted temporary board ID: %s", board_id)
            except HTTPStatusError as e:
                # Ignore 404 if board was already deleted by the test itself
                if e.response.status_code not in [404, 403]:
                    logger.error(
                        "HTTP error deleting temporary board %s: %s", board_id, e
                    )
                else:
                    logger.warning(
                        "Temporary board %s already deleted or access denied (%s).",
                        board_id,
                        e.response.status_code,
                    )
            except Exception as e:
                logger.error(
                    "Unexpected error deleting temporary board %s: %s", board_id, e
                )


@pytest.fixture
async def temporary_board_with_stack(nc_client: NextcloudClient, temporary_board: dict):
    """
    Fixture to create a temporary stack in a temporary board.
    Yields a tuple: (board_data, stack_data).
    Depends on the temporary_board fixture.
    """
    board_data = temporary_board
    board_id = board_data["id"]
    unique_suffix = uuid.uuid4().hex[:8]
    stack_title = f"Test Stack {unique_suffix}"
    stack_order = 1
    stack = None

    logger.info("Creating temporary stack in board ID: %s", board_id)
    try:
        stack = await nc_client.deck.create_stack(board_id, stack_title, stack_order)
        stack_data = {
            "id": stack.id,
            "title": stack.title,
            "order": stack.order,
            "boardId": board_id,
        }

        logger.info("Temporary stack created with ID: %s", stack.id)
        yield (board_data, stack_data)

    finally:
        # Clean up - delete stack
        if stack and hasattr(stack, "id"):
            logger.info("Cleaning up temporary stack ID: %s", stack.id)
            try:
                await nc_client.deck.delete_stack(board_id, stack.id)
                logger.info("Successfully deleted temporary stack ID: %s", stack.id)
            except HTTPStatusError as e:
                if e.response.status_code not in [404, 403]:
                    logger.error(
                        "HTTP error deleting temporary stack %s: %s", stack.id, e
                    )
                else:
                    logger.warning(
                        "Temporary stack %s already deleted or access denied (%s).",
                        stack.id,
                        e.response.status_code,
                    )
            except Exception as e:
                logger.error(
                    "Unexpected error deleting temporary stack %s: %s", stack.id, e
                )


@pytest.fixture
async def temporary_board_with_card(
    nc_client: NextcloudClient, temporary_board_with_stack: tuple
):
    """
    Fixture to create a temporary card in a temporary stack within a temporary board.
    Yields a tuple: (board_data, stack_data, card_data).
    Depends on the temporary_board_with_stack fixture.
    """
    board_data, stack_data = temporary_board_with_stack
    board_id = board_data["id"]
    stack_id = stack_data["id"]
    unique_suffix = uuid.uuid4().hex[:8]
    card_title = f"Test Card {unique_suffix}"
    card_description = f"Test description for card {unique_suffix}"
    card = None

    logger.info(
        "Creating temporary card in stack ID: %s, board ID: %s", stack_id, board_id
    )
    try:
        card = await nc_client.deck.create_card(
            board_id, stack_id, card_title, description=card_description
        )
        card_data = {
            "id": card.id,
            "title": card.title,
            "description": card.description,
            "stackId": stack_id,
            "boardId": board_id,
        }

        logger.info("Temporary card created with ID: %s", card.id)
        yield (board_data, stack_data, card_data)

    finally:
        # Clean up - delete card
        if card and hasattr(card, "id"):
            logger.info("Cleaning up temporary card ID: %s", card.id)
            try:
                await nc_client.deck.delete_card(board_id, stack_id, card.id)
                logger.info("Successfully deleted temporary card ID: %s", card.id)
            except HTTPStatusError as e:
                if e.response.status_code not in [404, 403]:
                    logger.error(
                        "HTTP error deleting temporary card %s: %s", card.id, e
                    )
                else:
                    logger.warning(
                        "Temporary card %s already deleted or access denied (%s).",
                        card.id,
                        e.response.status_code,
                    )
            except Exception as e:
                logger.error(
                    "Unexpected error deleting temporary card %s: %s", card.id, e
                )


@pytest.fixture
async def temporary_conversation(nc_client: NextcloudClient):
    """Create a temporary Talk conversation and clean it up after the test.

    Yields a dict with the room ``token``, ``id``, and ``name``.
    """
    unique_suffix = uuid.uuid4().hex[:8]
    room_name = f"MCP Test Room {unique_suffix}"
    token = None

    logger.info("Creating temporary Talk conversation: %s", room_name)
    try:
        room = await nc_client.talk.create_conversation(room_name=room_name)
        token = room.token
        logger.info("Temporary Talk conversation created (token=%s)", token)
        yield {"token": token, "id": room.id, "name": room_name}

    finally:
        if token:
            logger.info("Cleaning up temporary Talk conversation token=%s", token)
            try:
                await nc_client.talk.delete_conversation(token)
                logger.info("Successfully deleted Talk conversation token=%s", token)
            except HTTPStatusError as e:
                if e.response.status_code not in [404, 403]:
                    logger.error(
                        "HTTP error deleting Talk conversation %s: %s", token, e
                    )
                else:
                    logger.warning(
                        "Talk conversation %s already deleted or access denied (%s).",
                        token,
                        e.response.status_code,
                    )
            except Exception as e:
                logger.error(
                    "Unexpected error deleting Talk conversation %s: %s", token, e
                )


@pytest.fixture(scope="session")
def shared_test_calendar_name():
    """Unique calendar name for the entire test session."""
    return f"test_calendar_shared_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def shared_test_calendar_name_2():
    """Second unique calendar name for cross-calendar tests."""
    return f"test_calendar_shared_2_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
async def shared_calendar(nc_client: NextcloudClient, shared_test_calendar_name: str):
    """Create a shared calendar for all tests in the session. Reuses the calendar to avoid rate limiting."""
    calendar_name = shared_test_calendar_name

    try:
        # Create a test calendar
        logger.info("Creating shared test calendar: %s", calendar_name)
        result = await nc_client.calendar.create_calendar(
            calendar_name=calendar_name,
            display_name=f"Shared Test Calendar {calendar_name}",
            description="Shared calendar for integration testing (reused across tests)",
            color="#FF5722",
        )

        if result["status_code"] not in [200, 201]:
            pytest.skip(f"Failed to create shared test calendar: {result}")

        logger.info("Created shared test calendar: %s", calendar_name)
        yield calendar_name

    except Exception as e:
        logger.error("Error setting up shared test calendar: %s", e)
        pytest.skip(f"Shared calendar setup failed: {e}")

    finally:
        # Cleanup: Delete the shared calendar at end of session
        try:
            logger.info("Cleaning up shared test calendar: %s", calendar_name)
            await nc_client.calendar.delete_calendar(calendar_name)
            logger.info("Successfully deleted shared test calendar: %s", calendar_name)
        except Exception as e:
            logger.error("Error deleting shared test calendar %s: %s", calendar_name, e)


@pytest.fixture(scope="session")
async def shared_calendar_2(
    nc_client: NextcloudClient,
    shared_test_calendar_name_2: str,
    shared_calendar: str,  # Explicit dependency to ensure proper initialization order
):
    """Create a second shared calendar for cross-calendar tests.

    Note: Depends on shared_calendar to ensure proper fixture initialization order
    and avoid race conditions when running multiple tests together.
    """
    calendar_name = shared_test_calendar_name_2

    try:
        # Wait for first calendar to fully initialize to avoid Nextcloud rate limiting
        # When creating multiple calendars rapidly, Nextcloud may not register them all

        logger.info("Waiting before creating second calendar to avoid rate limiting...")
        await anyio.sleep(3)  # Increased from 2 to 3 seconds

        # Create a test calendar
        logger.info("Creating second shared test calendar: %s", calendar_name)
        result = await nc_client.calendar.create_calendar(
            calendar_name=calendar_name,
            display_name=f"Shared Test Calendar 2 {calendar_name}",
            description="Second shared calendar for cross-calendar testing",
            color="#4CAF50",
        )

        if result["status_code"] not in [200, 201]:
            pytest.skip(f"Failed to create second shared test calendar: {result}")

        logger.info("Created second shared test calendar: %s", calendar_name)

        # Verify calendar was created by listing calendars
        # Add small delay to allow calendar to propagate in the system

        await anyio.sleep(1.0)  # Allow time for calendar to propagate

        calendars = await nc_client.calendar.list_calendars()
        calendar_names = [cal["name"] for cal in calendars]
        if calendar_name not in calendar_names:
            logger.warning(
                "Calendar %s not found immediately after creation. Available: %s",
                calendar_name,
                calendar_names,
            )
            # Try one more time after a longer delay
            await anyio.sleep(3)  # Additional wait for calendar synchronization
            calendars = await nc_client.calendar.list_calendars()
            calendar_names = [cal["name"] for cal in calendars]
            if calendar_name not in calendar_names:
                logger.error(
                    "Calendar %s still not found after retries. Available: %s",
                    calendar_name,
                    calendar_names,
                )
                pytest.fail(
                    f"Failed to create second shared calendar: {calendar_name} not found in listing"
                )

        logger.info(
            "Successfully verified second shared test calendar: %s", calendar_name
        )
        yield calendar_name

    except Exception as e:
        logger.error("Error setting up second shared test calendar: %s", e)
        pytest.skip(f"Second shared calendar setup failed: {e}")

    finally:
        # Cleanup: Delete the second shared calendar at end of session
        try:
            logger.info("Cleaning up second shared test calendar: %s", calendar_name)
            await nc_client.calendar.delete_calendar(calendar_name)
            logger.info(
                "Successfully deleted second shared test calendar: %s", calendar_name
            )
        except Exception as e:
            logger.error(
                "Error deleting second shared test calendar %s: %s", calendar_name, e
            )


@pytest.fixture
async def temporary_calendar(shared_calendar: str, nc_client: NextcloudClient):
    """Provide the shared calendar and clean up todos after each test.

    This fixture reuses a session-scoped calendar to avoid Nextcloud rate limiting
    on calendar creation. Each test gets the same calendar but todos are cleaned up
    between tests.
    """
    calendar_name = shared_calendar

    yield calendar_name

    # Cleanup: Delete all todos from this calendar
    try:
        logger.info("Cleaning up todos from shared calendar: %s", calendar_name)
        todos = await nc_client.calendar.list_todos(calendar_name)
        for todo in todos:
            try:
                await nc_client.calendar.delete_todo(calendar_name, todo["uid"])
            except Exception as e:
                logger.warning("Error deleting todo %s: %s", todo["uid"], e)
        logger.info("Cleaned up %s todos from shared calendar", len(todos))
    except Exception as e:
        logger.error("Error cleaning up todos from calendar %s: %s", calendar_name, e)


@pytest.fixture(scope="session")
async def nc_oauth_client(
    anyio_backend,
    playwright_oauth_token: str,
) -> AsyncGenerator[NextcloudClient, Any]:
    """
    Fixture to create a NextcloudClient instance using automated Playwright OAuth authentication.
    Uses headless browser automation suitable for CI/CD.
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")

    if not all([nextcloud_host, username]):
        pytest.skip("OAuth client fixture requires NEXTCLOUD_HOST and USERNAME")

    logger.info("Creating OAuth NextcloudClient (Playwright) for user: %s", username)
    client = NextcloudClient.from_token(
        base_url=nextcloud_host,
        token=playwright_oauth_token,
        username=username,
    )

    # Verify the OAuth client works
    try:
        await client.capabilities()
        logger.info(
            "OAuth NextcloudClient (Playwright) initialized and capabilities checked."
        )
        yield client
    except Exception as e:
        logger.error("Failed to initialize OAuth NextcloudClient (Playwright): %s", e)
        pytest.fail(f"Failed to connect to Nextcloud with Playwright OAuth token: {e}")
    finally:
        await client.close()


@pytest.fixture(scope="session")
def oauth_callback_server():
    """
    Fixture to create an HTTP server for OAuth callback handling.

    Supports multiple concurrent OAuth flows using state parameters for correlation.

    Yields a tuple of (auth_states, server_url) where:
    - auth_states: A dict mapping state parameter to auth code
    - server_url: The callback URL for the server (e.g., "http://localhost:8081")

    The server automatically shuts down when the fixture is torn down.
    """
    # Use a dict to store auth codes keyed by state parameter
    # This allows multiple concurrent OAuth flows
    auth_states = {}
    httpd = None
    server_thread = None

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Suppress default HTTP logging
            pass

        def do_GET(self):
            # Parse the callback request
            parsed_path = urlparse(self.path)
            query = parse_qs(parsed_path.query)
            code = query.get("code", [None])[0]
            state = query.get("state", [None])[0]

            # Only process if we have a valid code
            if code:
                # Store code keyed by state parameter for correlation
                if state:
                    auth_states[state] = code
                    logger.info(
                        "OAuth callback received for state=%s... Code: %s...",
                        state[:16],
                        code[:20],
                    )
                else:
                    # Fallback for flows without state parameter (legacy interactive flow)
                    auth_states["_default"] = code
                    logger.info(
                        "OAuth callback received (no state). Code: %s...", code[:20]
                    )

                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>Authentication successful!</h1><p>You can close this window.</p></body></html>"
                )
            else:
                # Ignore requests without a code (e.g., favicon requests)
                logger.debug("Ignoring request without auth code: %s", self.path)
                self.send_response(404)
                self.end_headers()

    try:
        # Start the HTTP server
        httpd = HTTPServer(("localhost", 8081), OAuthCallbackHandler)
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        logger.info("OAuth callback server started on http://localhost:8081")

        # Yield the auth states dict and server URL
        yield auth_states, "http://localhost:8081"

    finally:
        # Clean up the server
        if httpd:
            logger.info("Shutting down OAuth callback server...")
            shutdown_thread = threading.Thread(target=httpd.shutdown)
            shutdown_thread.start()
            shutdown_thread.join(timeout=2)  # Wait up to 2 seconds for shutdown
            httpd.server_close()
            logger.info("OAuth callback server shut down successfully")
        if server_thread:
            server_thread.join(timeout=1)


@pytest.fixture(scope="session")
async def shared_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """
    Fixture to obtain shared OAuth client credentials that will be reused for all users.

    Creates an opaque token OAuth client with allowed_scopes for the standard OAuth MCP
    server (port 8001). While opaque tokens don't embed scopes, the allowed_scopes
    configuration ensures tokens have proper scopes when introspected.

    The client is automatically deleted from Nextcloud after the test session completes.

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
    from nextcloud_mcp_server.auth.client_registration import delete_client

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Shared OAuth client requires NEXTCLOUD_HOST")

    # Get callback URL from the real callback server
    auth_states, callback_url = oauth_callback_server

    logger.info("Setting up shared OAuth client credentials for all test users...")
    logger.info("Using real callback server at: %s", callback_url)

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        # OIDC Discovery
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await http_client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        token_endpoint = oidc_config.get("token_endpoint")
        authorization_endpoint = oidc_config.get("authorization_endpoint")

        if not token_endpoint or not authorization_endpoint:
            raise ValueError(
                "OIDC discovery missing required endpoints (token_endpoint or authorization_endpoint)"
            )

        # Create opaque token client with allowed_scopes (not JWT)
        # This ensures the token has proper scopes even though they're not embedded
        client_info = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Pytest - Shared Test Client (Opaque)",
            allowed_scopes=DEFAULT_FULL_SCOPES,
            token_type="Bearer",  # Opaque tokens for port 8001
        )

        logger.info("Shared OAuth client ready: %s...", client_info.client_id[:16])
        logger.info(
            "This opaque token client with full scopes will be reused for all test user authentications"
        )

        yield (
            client_info.client_id,
            client_info.client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )

        # Cleanup: Delete OAuth client from Nextcloud using RFC 7592
        try:
            logger.info(
                "Cleaning up shared OAuth client: %s...", client_info.client_id[:16]
            )
            success = await delete_client(
                nextcloud_url=nextcloud_host,
                client_id=client_info.client_id,
                registration_access_token=client_info.registration_access_token,
                client_secret=client_info.client_secret,
                registration_client_uri=client_info.registration_client_uri,
            )
            if success:
                logger.info(
                    "Successfully deleted shared OAuth client: %s...",
                    client_info.client_id[:16],
                )
            else:
                logger.warning(
                    "Failed to delete shared OAuth client: %s...",
                    client_info.client_id[:16],
                )
        except Exception as e:
            logger.warning(
                "Error cleaning up shared OAuth client %s...: %s",
                client_info.client_id[:16],
                e,
            )


@pytest.fixture(scope="session")
async def shared_jwt_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """
    Fixture to obtain shared JWT OAuth client credentials for testing JWT token behavior.

    Creates a JWT OAuth client with full scopes (all app read/write scopes). The client
    is configured with token_type="JWT" to request JWT-formatted access tokens from the
    OIDC server (instead of opaque tokens).

    The client is automatically deleted from Nextcloud after the test session completes.

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
    from nextcloud_mcp_server.auth.client_registration import delete_client

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Shared JWT OAuth client requires NEXTCLOUD_HOST")

    # Get callback URL from the real callback server
    auth_states, callback_url = oauth_callback_server

    logger.info("Setting up shared JWT OAuth client credentials...")
    logger.info("Using real callback server at: %s", callback_url)

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        # OIDC Discovery
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await http_client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        token_endpoint = oidc_config.get("token_endpoint")
        authorization_endpoint = oidc_config.get("authorization_endpoint")

        if not token_endpoint or not authorization_endpoint:
            raise ValueError(
                "OIDC discovery missing required endpoints (token_endpoint or authorization_endpoint)"
            )

        # Create JWT client with full scopes (all app read/write scopes)
        client_info = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Pytest - Shared JWT Test Client",
            allowed_scopes=DEFAULT_FULL_SCOPES,
            token_type="JWT",  # Explicitly set JWT token type
        )

        logger.info("Shared JWT OAuth client ready: %s...", client_info.client_id[:16])
        logger.info(
            "This JWT client with full scopes will be reused for JWT MCP server tests"
        )

        yield (
            client_info.client_id,
            client_info.client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )

        # Cleanup: Delete OAuth client from Nextcloud using RFC 7592
        try:
            logger.info(
                "Cleaning up shared JWT OAuth client: %s...", client_info.client_id[:16]
            )
            success = await delete_client(
                nextcloud_url=nextcloud_host,
                client_id=client_info.client_id,
                registration_access_token=client_info.registration_access_token,
                client_secret=client_info.client_secret,
                registration_client_uri=client_info.registration_client_uri,
            )
            if success:
                logger.info(
                    "Successfully deleted shared JWT OAuth client: %s...",
                    client_info.client_id[:16],
                )
            else:
                logger.warning(
                    "Failed to delete shared JWT OAuth client: %s...",
                    client_info.client_id[:16],
                )
        except Exception as e:
            logger.warning(
                "Error cleaning up shared JWT OAuth client %s...: %s",
                client_info.client_id[:16],
                e,
            )


async def get_mcp_server_resource_metadata(mcp_base_url: str) -> dict:
    """
    Fetch MCP server's Protected Resource Metadata (RFC 9470).

    This retrieves the MCP server's resource information including:
    - resource: The MCP server's client ID (used as audience for tokens)
    - authorization_servers: List of trusted OAuth servers
    - scopes_supported: Available scopes

    Args:
        mcp_base_url: Base URL of the MCP server (e.g., "http://localhost:8001")
                      WITHOUT the /mcp path component

    Returns:
        Dict with resource metadata

    Raises:
        HTTPStatusError: If metadata endpoint is not available
    """
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        prm_url = f"{mcp_base_url}/.well-known/oauth-protected-resource"
        logger.debug("Fetching resource metadata from: %s", prm_url)

        response = await http_client.get(prm_url)
        response.raise_for_status()
        metadata = response.json()

        logger.debug("Resource metadata: %s", metadata)
        return metadata


async def _create_oauth_client_with_scopes(
    callback_url: str,
    client_name: str,
    allowed_scopes: str,
    token_type: str = "JWT",
):
    """
    Helper function to create an OAuth client with specific allowed_scopes using DCR.

    Args:
        callback_url: OAuth callback URL
        client_name: Name of the OAuth client
        allowed_scopes: Space-separated list of allowed scopes
        token_type: Either "JWT" or "Bearer" (default: "JWT")

    Returns:
        ClientInfo object with full registration details including registration_access_token
    """
    from nextcloud_mcp_server.auth.client_registration import register_client

    logger.info(
        "Creating %s OAuth client '%s' with scopes: %s using DCR",
        token_type,
        client_name,
        allowed_scopes,
    )

    # Get Nextcloud host and registration endpoint
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        raise ValueError("NEXTCLOUD_HOST environment variable not set")

    # Discover registration endpoint
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await http_client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()
        registration_endpoint = oidc_config.get("registration_endpoint")

        if not registration_endpoint:
            raise ValueError("OIDC discovery missing registration_endpoint")

    # Register client using DCR
    client_info = await register_client(
        nextcloud_url=nextcloud_host,
        registration_endpoint=registration_endpoint,
        client_name=client_name,
        redirect_uris=[callback_url],
        scopes=allowed_scopes,
        token_type=token_type,
    )

    logger.info(
        "Created OAuth client via DCR: %s... with scopes: %s",
        client_info.client_id[:16],
        allowed_scopes,
    )
    if client_info.registration_access_token:
        logger.info(
            "RFC 7592 registration_access_token received - client can be deleted"
        )
    else:
        logger.warning("No registration_access_token - client deletion may fail")

    return client_info


@pytest.fixture(scope="session")
async def read_only_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """
    Fixture for OAuth client with only read scopes.

    The client is automatically deleted from Nextcloud after the test session completes.

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
    from nextcloud_mcp_server.auth.client_registration import delete_client

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Read-only OAuth client requires NEXTCLOUD_HOST")

    auth_states, callback_url = oauth_callback_server

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await http_client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        token_endpoint = oidc_config.get("token_endpoint")
        authorization_endpoint = oidc_config.get("authorization_endpoint")

        # Create JWT client with READ-ONLY scopes
        client_info = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Test Client Read Only",
            allowed_scopes=DEFAULT_READ_SCOPES,
            token_type="JWT",  # JWT tokens for scope validation
        )

        yield (
            client_info.client_id,
            client_info.client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )

        # Cleanup: Delete OAuth client from Nextcloud using RFC 7592
        try:
            logger.info(
                "Cleaning up read-only OAuth client: %s...", client_info.client_id[:16]
            )
            success = await delete_client(
                nextcloud_url=nextcloud_host,
                client_id=client_info.client_id,
                registration_access_token=client_info.registration_access_token,
                client_secret=client_info.client_secret,
                registration_client_uri=client_info.registration_client_uri,
            )
            if success:
                logger.info(
                    "Successfully deleted read-only OAuth client: %s...",
                    client_info.client_id[:16],
                )
            else:
                logger.warning(
                    "Failed to delete read-only OAuth client: %s...",
                    client_info.client_id[:16],
                )
        except Exception as e:
            logger.warning(
                "Error cleaning up read-only OAuth client %s...: %s",
                client_info.client_id[:16],
                e,
            )


@pytest.fixture(scope="session")
async def write_only_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """
    Fixture for OAuth client with only write scopes.

    The client is automatically deleted from Nextcloud after the test session completes.

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
    from nextcloud_mcp_server.auth.client_registration import delete_client

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Write-only OAuth client requires NEXTCLOUD_HOST")

    auth_states, callback_url = oauth_callback_server

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await http_client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        token_endpoint = oidc_config.get("token_endpoint")
        authorization_endpoint = oidc_config.get("authorization_endpoint")

        # Create JWT client with WRITE-ONLY scopes
        client_info = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Test Client Write Only",
            allowed_scopes=DEFAULT_WRITE_SCOPES,
            token_type="JWT",  # JWT tokens for scope validation
        )

        yield (
            client_info.client_id,
            client_info.client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )

        # Cleanup: Delete OAuth client from Nextcloud using RFC 7592
        try:
            logger.info(
                "Cleaning up write-only OAuth client: %s...", client_info.client_id[:16]
            )
            success = await delete_client(
                nextcloud_url=nextcloud_host,
                client_id=client_info.client_id,
                registration_access_token=client_info.registration_access_token,
                client_secret=client_info.client_secret,
                registration_client_uri=client_info.registration_client_uri,
            )
            if success:
                logger.info(
                    "Successfully deleted write-only OAuth client: %s...",
                    client_info.client_id[:16],
                )
            else:
                logger.warning(
                    "Failed to delete write-only OAuth client: %s...",
                    client_info.client_id[:16],
                )
        except Exception as e:
            logger.warning(
                "Error cleaning up write-only OAuth client %s...: %s",
                client_info.client_id[:16],
                e,
            )


@pytest.fixture(scope="session")
async def full_access_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """
    Fixture for OAuth client with both read and write scopes.

    The client is automatically deleted from Nextcloud after the test session completes.

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
    from nextcloud_mcp_server.auth.client_registration import delete_client

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Full-access OAuth client requires NEXTCLOUD_HOST")

    auth_states, callback_url = oauth_callback_server

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await http_client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        token_endpoint = oidc_config.get("token_endpoint")
        authorization_endpoint = oidc_config.get("authorization_endpoint")

        # Create JWT client with FULL ACCESS (both read and write scopes)
        client_info = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Test Client Full Access",
            allowed_scopes=DEFAULT_FULL_SCOPES,
            token_type="JWT",  # JWT tokens for scope validation
        )

        yield (
            client_info.client_id,
            client_info.client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )

        # Cleanup: Delete OAuth client from Nextcloud using RFC 7592
        try:
            logger.info(
                "Cleaning up full-access OAuth client: %s...",
                client_info.client_id[:16],
            )
            success = await delete_client(
                nextcloud_url=nextcloud_host,
                client_id=client_info.client_id,
                registration_access_token=client_info.registration_access_token,
                client_secret=client_info.client_secret,
                registration_client_uri=client_info.registration_client_uri,
            )
            if success:
                logger.info(
                    "Successfully deleted full-access OAuth client: %s...",
                    client_info.client_id[:16],
                )
            else:
                logger.warning(
                    "Failed to delete full-access OAuth client: %s...",
                    client_info.client_id[:16],
                )
        except Exception as e:
            logger.warning(
                "Error cleaning up full-access OAuth client %s...: %s",
                client_info.client_id[:16],
                e,
            )


@pytest.fixture(scope="session")
async def no_custom_scopes_oauth_client_credentials(
    anyio_backend, oauth_callback_server
):
    """
    Fixture for OAuth client with NO custom scopes (only OIDC defaults).

    Tests the security behavior when a user grants only the default OIDC scopes
    (openid, profile, email) but declines custom application scopes (notes.read, notes.write, etc.).

    The client is automatically deleted from Nextcloud after the test session completes.

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
    from nextcloud_mcp_server.auth.client_registration import delete_client

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("No-custom-scopes OAuth client requires NEXTCLOUD_HOST")

    auth_states, callback_url = oauth_callback_server

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await http_client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        token_endpoint = oidc_config.get("token_endpoint")
        authorization_endpoint = oidc_config.get("authorization_endpoint")

        # Create JWT client with NO custom scopes (only OIDC defaults)
        client_info = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Test Client No Custom Scopes",
            allowed_scopes="openid profile email",  # No app-specific scopes (no app access)
            token_type="JWT",  # JWT tokens for scope validation
        )

        yield (
            client_info.client_id,
            client_info.client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )

        # Cleanup: Delete OAuth client from Nextcloud using RFC 7592
        try:
            logger.info(
                "Cleaning up no-custom-scopes OAuth client: %s...",
                client_info.client_id[:16],
            )
            success = await delete_client(
                nextcloud_url=nextcloud_host,
                client_id=client_info.client_id,
                registration_access_token=client_info.registration_access_token,
                client_secret=client_info.client_secret,
                registration_client_uri=client_info.registration_client_uri,
            )
            if success:
                logger.info(
                    "Successfully deleted no-custom-scopes OAuth client: %s...",
                    client_info.client_id[:16],
                )
            else:
                logger.warning(
                    "Failed to delete no-custom-scopes OAuth client: %s...",
                    client_info.client_id[:16],
                )
        except Exception as e:
            logger.warning(
                "Error cleaning up no-custom-scopes OAuth client %s...: %s",
                client_info.client_id[:16],
                e,
            )


@pytest.fixture(scope="session")
async def playwright_oauth_token(
    anyio_backend, browser, shared_oauth_client_credentials, oauth_callback_server
) -> str:
    """
    Fixture to obtain an OAuth access token using Playwright headless browser automation.

    This fully automates the OAuth flow by:
    1. Using shared OAuth client credentials (reused across all users)
    2. Navigating to authorization URL in headless browser
    3. Programmatically filling in login form
    4. Handling OAuth consent
    5. Waiting for callback server to receive auth code (NEW: using real callback server!)
    6. Exchanging code for access token

    Environment variables required:
    - NEXTCLOUD_HOST: Nextcloud instance URL
    - NEXTCLOUD_USERNAME: Username for login
    - NEXTCLOUD_PASSWORD: Password for login

    Playwright Configuration:
    - Configure browser via pytest CLI args: --browser firefox --headed
    - Browser fixture provided by pytest-playwright-asyncio
    - See: https://playwright.dev/python/docs/test-runners
    """

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")
    password = os.getenv("NEXTCLOUD_PASSWORD")

    if not all([nextcloud_host, username, password]):
        pytest.skip(
            "Playwright OAuth requires NEXTCLOUD_HOST, NEXTCLOUD_USERNAME, and NEXTCLOUD_PASSWORD"
        )

    # Get auth_states dict from callback server
    auth_states, _ = oauth_callback_server

    # Unpack shared client credentials
    client_id, client_secret, callback_url, token_endpoint, authorization_endpoint = (
        shared_oauth_client_credentials
    )

    logger.info("Starting Playwright-based OAuth flow for %s...", username)
    logger.info("Using shared OAuth client: %s...", client_id[:16])
    logger.info("Using real callback server at: %s", callback_url)

    # Fetch MCP server's resource metadata to get correct audience
    mcp_server_base_url = "http://localhost:8001"
    try:
        resource_metadata = await get_mcp_server_resource_metadata(mcp_server_base_url)
        resource_id = resource_metadata.get("resource")
        if resource_id:
            logger.info(
                "MCP server resource ID (for audience): %s...", resource_id[:16]
            )
        else:
            logger.warning("No resource ID in metadata - token may have wrong audience")
    except Exception as e:
        logger.warning("Failed to fetch resource metadata: %s", e)
        resource_id = None

    # Generate unique state parameter for this OAuth flow
    state = secrets.token_urlsafe(32)
    logger.debug("Generated state: %s...", state[:16])

    # Construct authorization URL with state and resource parameters
    auth_url = (
        f"{authorization_endpoint}?"
        f"response_type=code&"
        f"client_id={client_id}&"
        f"redirect_uri={quote(callback_url, safe='')}&"
        f"state={state}&"
        f"scope=openid%20profile%20email%20notes.read%20notes.write%20calendar.read%20calendar.write%20contacts.read%20contacts.write%20cookbook.read%20cookbook.write%20deck.read%20deck.write%20tables.read%20tables.write%20files.read%20files.write%20sharing.read%20sharing.write"
    )

    # Add resource parameter (RFC 8707) if available
    if resource_id:
        auth_url += f"&resource={quote(resource_id, safe='')}"
        logger.debug("Added resource parameter to auth URL: %s...", resource_id[:16])

    # Async browser automation using pytest-playwright's browser fixture
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        # Navigate to authorization URL
        logger.debug("Navigating to: %s", auth_url)
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)

        # Check if we need to login first
        current_url = page.url
        logger.debug("Current URL after navigation: %s", current_url)

        # If we're on a login page, fill in credentials
        if "/login" in current_url or "/index.php/login" in current_url:
            logger.info("Login page detected, filling in credentials...")

            # Wait for login form
            await page.wait_for_selector('input[name="user"]', timeout=10000)

            # Fill in username and password
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)

            logger.debug("Credentials filled, submitting login form...")

            # Submit the form
            await page.click('button[type="submit"]')

            # Wait for navigation after login
            await page.wait_for_load_state("networkidle", timeout=60000)
            current_url = page.url
            logger.info("After login, current URL: %s", current_url)

        # Wait for the OIDC redirect chain to settle before handling consent.
        # After login, the flow goes: /apps/oidc/redirect (JS page) → JS navigates
        # to /authorize → 303 to /consent. networkidle fires after the JS page
        # loads but before the JS navigation starts.
        logger.info("Waiting for OIDC redirect chain to settle...")
        settle_start = time.time()
        while time.time() - settle_start < 15:
            current_url = page.url
            if "/consent" in current_url or "localhost:8081" in current_url:
                break
            await anyio.sleep(0.5)

        # Handle consent screen if present
        if "/consent" in page.url:
            await page.wait_for_load_state("networkidle", timeout=10000)
            await _handle_oauth_consent_screen(page, username)
        else:
            logger.debug("No consent screen (URL: %s)", page.url)

        # Wait for callback server to receive the auth code
        # Browser will be redirected to localhost:8081 which will capture the code
        logger.info("Waiting for callback server to receive auth code...")
        timeout_seconds = 30
        start_time = time.time()
        while state not in auth_states:
            if time.time() - start_time > timeout_seconds:
                # Take a screenshot for debugging
                screenshot_path = "/tmp/playwright_oauth_error.png"
                await page.screenshot(path=screenshot_path)
                logger.error("Screenshot saved to %s", screenshot_path)
                raise TimeoutError(
                    f"Timeout waiting for OAuth callback (state={state[:16]}...)"
                )
            await anyio.sleep(0.5)

        auth_code = auth_states[state]
        logger.info("Successfully received authorization code: %s...", auth_code[:20])

    finally:
        await context.close()

    # Exchange authorization code for access token
    logger.info("Exchanging authorization code for access token...")
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
        access_token = token_data.get("access_token")

        if not access_token:
            raise ValueError(f"No access_token in response: {token_data}")

        logger.info("Successfully obtained OAuth access token via Playwright")
        return access_token


@pytest.fixture(scope="session")
async def playwright_oauth_token_jwt(
    anyio_backend, browser, shared_jwt_oauth_client_credentials, oauth_callback_server
) -> str:
    """
    Fixture to obtain a JWT OAuth access token for the JWT MCP server.

    Uses a JWT OAuth client with full scopes (all app read/write scopes) to ensure
    the access token includes proper scope claims that the JWT MCP server can validate.

    Returns:
        JWT access token string
    """
    return await _get_oauth_token_with_scopes(
        browser,
        shared_jwt_oauth_client_credentials,
        oauth_callback_server,
        scopes=DEFAULT_FULL_SCOPES,
    )


async def _handle_oauth_consent_screen(page, username: str = "user"):
    """
    Handle the OIDC consent screen that appears during OAuth flow.

    The consent screen:
    - Has a #oidc-consent div with data attributes (client-name, scopes, client-id)
    - Uses Vue.js to dynamically render scope checkboxes
    - Has "Allow" and "Deny" buttons

    This function:
    1. Checks if we're on a consent screen (look for #oidc-consent div)
    2. Waits for Vue.js to render the content (wait for "Allow" button)
    3. Logs available scopes (for debugging)
    4. Clicks the "Allow" button to grant consent

    Args:
        page: Playwright page instance
        username: Username for logging purposes

    Returns:
        True if consent was handled, False if no consent screen was found
    """
    try:
        # Check if consent screen is present
        consent_div = await page.query_selector("#oidc-consent")

        if not consent_div:
            logger.debug("No consent screen found for %s", username)
            return False

        logger.info("Consent screen detected for %s", username)

        # Get consent screen data attributes
        client_name = await consent_div.get_attribute("data-client-name")
        scopes_attr = await consent_div.get_attribute("data-scopes")
        logger.info("  Client: %s", client_name)
        logger.info("  Requested scopes: %s", scopes_attr)

        # Wait for Vue.js to render the Allow button (max 10 seconds)
        try:
            await page.wait_for_selector('button:has-text("Allow")', timeout=10000)
            logger.info("  Allow button rendered by Vue.js")
        except Exception as e:
            logger.warning("  Timeout waiting for Allow button: %s", e)
            # Take a screenshot for debugging
            screenshot_path = f"/tmp/consent_no_allow_button_{username}.png"
            await page.screenshot(path=screenshot_path)
            logger.error("  Screenshot saved to %s", screenshot_path)
            raise

        # Check all scope checkboxes
        scope_checkboxes = await page.query_selector_all('input[type="checkbox"]')
        if scope_checkboxes:
            logger.info("  Found %s scope checkboxes", len(scope_checkboxes))
            for i, checkbox in enumerate(scope_checkboxes):
                # Check if checkbox is not already checked
                is_checked = await checkbox.is_checked()
                is_disabled = await checkbox.is_disabled()
                if not is_checked and not is_disabled:
                    await checkbox.check()
                    logger.info("    ✓ Checked scope checkbox %s", i + 1)
                elif is_checked:
                    logger.info("    ✓ Scope checkbox %s already checked", i + 1)
                elif is_disabled:
                    logger.info(
                        "    ⊗ Scope checkbox %s disabled (required scope)", i + 1
                    )

        # Click the Allow button to grant consent with retry logic.
        # Uses Playwright's native click (dispatches proper browser events that
        # trigger Vue.js handlers) instead of JS btn.click() which can miss them.
        allow_button = page.locator('button:has-text("Allow")')

        if await allow_button.count() > 0:
            logger.info("  Clicking Allow button to grant consent for %s...", username)

            for attempt in range(3):
                await allow_button.scroll_into_view_if_needed()
                await allow_button.click()
                try:
                    await page.wait_for_url(
                        lambda url: "/consent" not in url, timeout=10000
                    )
                    logger.info("  Consent granted for %s", username)
                    return True
                except (TimeoutError, PlaywrightTimeoutError):
                    if attempt == 2:
                        screenshot_path = f"/tmp/consent_click_failed_{username}.png"
                        await page.screenshot(path=screenshot_path)
                        logger.error(
                            "  Consent click failed after 3 attempts for %s, screenshot: %s",
                            username,
                            screenshot_path,
                        )
                        raise
                    logger.warning(
                        "  Consent click attempt %s didn't navigate, retrying...",
                        attempt + 1,
                    )

            raise RuntimeError("consent click retry loop exited unexpectedly")
        else:
            logger.error("  Allow button not found for %s", username)
            return False

    except Exception as e:
        logger.error("Error handling consent screen for %s: %s", username, e)
        raise


async def _get_oauth_token_with_scopes(
    browser,
    shared_oauth_client_credentials,
    oauth_callback_server,
    scopes: str,
    resource: str | None = None,
    mcp_server_base_url: str = "http://localhost:8004",  # login-flow container port
) -> str:
    """
    Helper function to obtain OAuth token with specific scopes.

    Args:
        browser: Playwright browser instance
        shared_oauth_client_credentials: Tuple of OAuth client credentials
        oauth_callback_server: OAuth callback server fixture
        scopes: Space-separated list of scopes (e.g., "openid profile email notes.read")
        resource: Optional resource parameter (RFC 8707) for token audience
        mcp_server_base_url: Base URL of the MCP server for resource metadata discovery

    Returns:
        OAuth access token string with requested scopes
    """

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")
    password = os.getenv("NEXTCLOUD_PASSWORD")

    if not all([nextcloud_host, username, password]):
        pytest.skip(
            "Scoped OAuth requires NEXTCLOUD_HOST, NEXTCLOUD_USERNAME, and NEXTCLOUD_PASSWORD"
        )

    # Get auth_states dict from callback server
    auth_states, _ = oauth_callback_server

    # Unpack shared client credentials
    client_id, client_secret, callback_url, token_endpoint, authorization_endpoint = (
        shared_oauth_client_credentials
    )

    logger.info("Starting Playwright-based OAuth flow with scopes: %s", scopes)
    logger.info("Using shared OAuth client: %s...", client_id[:16])
    logger.info("Using real callback server at: %s", callback_url)

    # If no resource provided, fetch from MCP server metadata
    if resource is None:
        try:
            resource_metadata = await get_mcp_server_resource_metadata(
                mcp_server_base_url
            )
            resource = resource_metadata.get("resource")
            if resource:
                logger.info(
                    "MCP server resource ID (for audience): %s...", resource[:16]
                )
            else:
                logger.warning(
                    "No resource ID in metadata - token may have wrong audience"
                )
        except Exception as e:
            logger.warning("Failed to fetch resource metadata: %s", e)

    # Generate unique state parameter for this OAuth flow
    state = secrets.token_urlsafe(32)
    logger.debug("Generated state: %s...", state[:16])

    # URL-encode scopes
    scopes_encoded = quote(scopes, safe="")

    # Construct authorization URL with state parameter and requested scopes
    auth_url = (
        f"{authorization_endpoint}?"
        f"response_type=code&"
        f"client_id={client_id}&"
        f"redirect_uri={quote(callback_url, safe='')}&"
        f"state={state}&"
        f"scope={scopes_encoded}"
    )

    # Add resource parameter (RFC 8707) if available
    if resource:
        auth_url += f"&resource={quote(resource, safe='')}"
        logger.debug("Added resource parameter to auth URL: %s...", resource[:16])

    # Async browser automation using pytest-playwright's browser fixture
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        # Navigate to authorization URL
        logger.debug("Navigating to: %s", auth_url)
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)

        # Check if we need to login first
        current_url = page.url
        logger.debug("Current URL after navigation: %s", current_url)

        # If we're on a login page, fill in credentials
        if "/login" in current_url or "/index.php/login" in current_url:
            logger.info("Login page detected, filling in credentials...")

            # Wait for login form
            await page.wait_for_selector('input[name="user"]', timeout=10000)

            # Fill in username and password
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)

            logger.debug("Credentials filled, submitting login form...")

            # Submit the form
            await page.click('button[type="submit"]')

            # Wait for navigation after login
            await page.wait_for_load_state("networkidle", timeout=60000)
            current_url = page.url
            logger.info("After login, current URL: %s", current_url)

        # Wait for the OIDC redirect chain to settle before handling consent.
        logger.info("Waiting for OIDC redirect chain to settle for %s...", username)
        settle_start = time.time()
        while time.time() - settle_start < 15:
            current_url = page.url
            if "/consent" in current_url or "localhost:8081" in current_url:
                break
            await anyio.sleep(0.5)

        # Handle consent screen if present
        if "/consent" in page.url:
            await page.wait_for_load_state("networkidle", timeout=10000)
            await _handle_oauth_consent_screen(page, username)
        else:
            logger.debug("No consent screen for %s (URL: %s)", username, page.url)

        # Wait for callback server to receive the auth code
        logger.info("Waiting for auth code with state: %s...", state[:16])
        start_time = time.time()
        timeout = 30

        while time.time() - start_time < timeout:
            if state in auth_states:
                auth_code = auth_states[state]
                logger.info("Auth code received from callback server")
                break
            await anyio.sleep(0.1)
        else:
            raise TimeoutError(
                f"Auth code not received within {timeout}s. State: {state[:16]}..."
            )

    finally:
        await context.close()

    # Exchange authorization code for access token
    logger.info("Exchanging authorization code for access token...")
    async with httpx.AsyncClient(timeout=30.0) as token_client:
        token_response = await token_client.post(
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
        access_token = token_data.get("access_token")

        if not access_token:
            raise ValueError(f"No access_token in response: {token_data}")

        logger.info("Successfully obtained OAuth access token with scopes: %s", scopes)
        return access_token


@pytest.fixture(scope="session")
async def playwright_oauth_token_read_only(
    anyio_backend, browser, read_only_oauth_client_credentials, oauth_callback_server
) -> str:
    """
    Fixture to obtain an OAuth access token with only read scopes.

    This token will only be able to perform read operations and should
    have write tools filtered out from the tool list.

    Uses a dedicated OAuth client with allowed_scopes=DEFAULT_READ_SCOPES
    """
    return await _get_oauth_token_with_scopes(
        browser,
        read_only_oauth_client_credentials,
        oauth_callback_server,
        scopes=DEFAULT_READ_SCOPES,
    )


@pytest.fixture(scope="session")
async def playwright_oauth_token_write_only(
    anyio_backend, browser, write_only_oauth_client_credentials, oauth_callback_server
) -> str:
    """
    Fixture to obtain an OAuth access token with only write scopes.

    This token will only be able to perform write operations and should
    have read tools filtered out from the tool list.

    Uses a dedicated OAuth client with allowed_scopes=DEFAULT_WRITE_SCOPES
    """
    return await _get_oauth_token_with_scopes(
        browser,
        write_only_oauth_client_credentials,
        oauth_callback_server,
        scopes=DEFAULT_WRITE_SCOPES,
    )


@pytest.fixture(scope="session")
async def playwright_oauth_token_full_access(
    anyio_backend, browser, full_access_oauth_client_credentials, oauth_callback_server
) -> str:
    """
    Fixture to obtain an OAuth access token with both read and write scopes.

    This token will be able to perform all operations.

    Uses a dedicated JWT OAuth client with allowed_scopes=DEFAULT_FULL_SCOPES
    """
    return await _get_oauth_token_with_scopes(
        browser,
        full_access_oauth_client_credentials,
        oauth_callback_server,
        scopes=DEFAULT_FULL_SCOPES,
    )


@pytest.fixture(scope="session")
async def playwright_oauth_token_no_custom_scopes(
    anyio_backend,
    browser,
    no_custom_scopes_oauth_client_credentials,
    oauth_callback_server,
) -> str:
    """
    Fixture to obtain an OAuth access token with NO custom scopes.

    Tests the security behavior when a user grants only default OIDC scopes
    (openid, profile, email) but declines application-specific scopes.

    Expected: JWT token will contain only default scopes, and all MCP tools
    should be filtered out since they all require app-specific scopes.

    Uses a dedicated JWT OAuth client with allowed_scopes="openid profile email"
    """
    return await _get_oauth_token_with_scopes(
        browser,
        no_custom_scopes_oauth_client_credentials,
        oauth_callback_server,
        scopes="openid profile email",  # Only OIDC defaults, no custom scopes
    )


@pytest.fixture(scope="session")
async def test_users_setup(anyio_backend, nc_client: NextcloudClient):
    """
    Create test users for multi-user OAuth testing.

    Creates four test users:
    - alice: Owner role, creates resources
    - bob: Viewer role, read-only access
    - charlie: Editor role, can edit (in 'editors' group)
    - diana: No-access role, no shares
    """
    test_user_configs = {
        "alice": {
            "password": "AliceSecurePass123!",
            "email": "alice@example.com",
            "display_name": "Alice Owner",
            "groups": [],
        },
        "bob": {
            "password": "BobSecurePass456!",
            "email": "bob@example.com",
            "display_name": "Bob Viewer",
            "groups": [],
        },
        "charlie": {
            "password": "CharlieSecurePass789!",
            "email": "charlie@example.com",
            "display_name": "Charlie Editor",
            "groups": ["editors"],
        },
        "diana": {
            "password": "DianaSecurePass012!",
            "email": "diana@example.com",
            "display_name": "Diana NoAccess",
            "groups": [],
        },
    }

    logger.info("=" * 60)
    logger.info("EXECUTING test_users_setup FIXTURE (session-scoped)")
    logger.info("Creating test users: %s", list(test_user_configs.keys()))
    logger.info("=" * 60)
    created_users = []

    try:
        # Create the 'editors' group first (charlie needs it)
        try:
            # Use admin nc_client to create the group via User API
            # First, try to create it (will fail if exists, but that's okay)
            async with httpx.AsyncClient() as http_client:
                base_url = str(nc_client._client.base_url)
                # Get password from environment since nc_client doesn't expose it
                password = os.getenv("NEXTCLOUD_PASSWORD")
                response = await http_client.post(
                    f"{base_url}/ocs/v2.php/cloud/groups",
                    auth=(nc_client.username, password),
                    headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                    data={"groupid": "editors"},
                )
                if response.status_code in [
                    200,
                    409,
                ]:  # 200 = created, 409 = already exists
                    logger.info("Editors group ready")
                else:
                    logger.warning(
                        "Group creation returned %s: %s",
                        response.status_code,
                        response.text,
                    )
        except Exception as e:
            logger.warning("Error creating editors group (may already exist): %s", e)

        # Create each test user (idempotent - check if exists first)
        for username, config in test_user_configs.items():
            # Check if user already exists
            user_exists = False
            try:
                await nc_client.users.get_user_details(username)
                user_exists = True
                logger.info("Test user %s already exists, skipping creation", username)
            except Exception:
                # User doesn't exist, proceed with creation
                pass

            if not user_exists:
                try:
                    await nc_client.users.create_user(
                        userid=username,
                        password=config["password"],
                        display_name=config["display_name"],
                        email=config["email"],
                    )
                    logger.info("Created test user: %s", username)
                    created_users.append(username)  # Only track users WE created

                    # Add user to groups if specified
                    for group in config["groups"]:
                        try:
                            await nc_client.users.add_user_to_group(username, group)
                            logger.info("Added %s to group %s", username, group)
                        except Exception as e:
                            logger.warning(
                                "Error adding %s to group %s: %s", username, group, e
                            )

                except Exception as e:
                    logger.warning("Could not create user %s: %s", username, e)

        logger.info("Test users setup complete: %s", created_users)
        yield test_user_configs

    finally:
        # Cleanup: delete test users
        logger.info("Cleaning up test users...")
        for username in created_users:
            try:
                await nc_client.users.delete_user(username)
                logger.info("Deleted test user: %s", username)
            except Exception as e:
                logger.warning("Error deleting test user %s: %s", username, e)

        # Clean up all app passwords from MCP server to prevent stale scanners
        import subprocess

        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "mcp-multi-user-basic",
                "sqlite3",
                "/app/data/tokens.db",
                "DELETE FROM app_passwords;",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to clean up app passwords (rc=%s): %s",
                result.returncode,
                result.stderr,
            )
        else:
            logger.info("Cleaned up all test app passwords")


async def _get_oauth_token_for_user(
    browser,
    shared_oauth_client_credentials,
    auth_states,
    username: str,
    password: str,
) -> str:
    """
    Helper function to get OAuth access token for a user via Playwright.

    Uses shared OAuth client credentials to authenticate multiple users with the same client.
    Now uses real callback server with state parameters for reliable token acquisition.

    Args:
        browser: Playwright browser instance
        shared_oauth_client_credentials: Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
        auth_states: Dict mapping state parameters to auth codes (from callback server)
        username: Username to authenticate as
        password: Password for the user

    Returns:
        OAuth access token string
    """

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")

    if not nextcloud_host:
        pytest.skip("OAuth requires NEXTCLOUD_HOST")

    # Unpack shared client credentials
    client_id, client_secret, callback_url, token_endpoint, authorization_endpoint = (
        shared_oauth_client_credentials
    )

    logger.info("Getting OAuth token for user: %s...", username)
    logger.info("Using shared OAuth client: %s...", client_id[:16])

    # Fetch resource identifier from PRM endpoint (RFC 9728)
    mcp_server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8001")
    prm_url = f"{mcp_server_url}/.well-known/oauth-protected-resource"

    logger.debug("Fetching PRM metadata from: %s", prm_url)
    async with httpx.AsyncClient() as client:
        prm_response = await client.get(prm_url, timeout=10)
        if prm_response.status_code != 200:
            logger.warning("Failed to fetch PRM metadata: %s", prm_response.status_code)
            # Fallback to default if PRM fetch fails
            mcp_server_resource = f"{mcp_server_url}/mcp"
        else:
            prm_data = prm_response.json()
            mcp_server_resource = prm_data.get("resource", f"{mcp_server_url}/mcp")
            logger.info("Using resource from PRM: %s", mcp_server_resource)

    # Generate unique state parameter for this OAuth flow
    state = secrets.token_urlsafe(32)
    logger.debug("Generated state for %s: %s...", username, state[:16])

    # Construct authorization URL with state parameter
    # Include resource parameter discovered from PRM endpoint
    auth_url = (
        f"{authorization_endpoint}?"
        f"response_type=code&"
        f"client_id={client_id}&"
        f"redirect_uri={quote(callback_url, safe='')}&"
        f"state={state}&"
        f"resource={quote(mcp_server_resource, safe='')}&"  # Resource URI from PRM
        f"scope=openid%20profile%20email%20notes.read%20notes.write%20calendar.read%20calendar.write%20contacts.read%20contacts.write%20cookbook.read%20cookbook.write%20deck.read%20deck.write%20tables.read%20tables.write%20files.read%20files.write%20sharing.read%20sharing.write"
    )

    logger.info("Performing browser OAuth flow for %s...", username)
    logger.debug("Authorization URL: %s", auth_url)

    # Browser automation
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        await page.goto(auth_url, wait_until="networkidle", timeout=30000)
        current_url = page.url

        # Login if needed
        if "/login" in current_url or "/index.php/login" in current_url:
            logger.info("Logging in as %s...", username)
            await page.wait_for_selector('input[name="user"]', timeout=10000)
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=30000)
            current_url = page.url

        # Wait for the OIDC redirect chain to settle before handling consent.
        # After login, the flow goes: /apps/oidc/redirect (JS page) → JS navigates
        # to /authorize → 303 to /consent. networkidle fires after the JS page
        # loads but before the JS navigation starts, so we must wait for the URL
        # to reach either the consent page or the callback.
        logger.info("Waiting for OIDC redirect chain to settle for %s...", username)
        settle_start = time.time()
        while time.time() - settle_start < 15:
            current_url = page.url
            if "/consent" in current_url or "localhost:8081" in current_url:
                break
            await anyio.sleep(0.5)
        else:
            logger.warning(
                "OIDC redirect chain did not settle for %s, current URL: %s",
                username,
                page.url,
            )

        # Handle consent screen if present
        if "/consent" in page.url:
            await page.wait_for_load_state("networkidle", timeout=10000)
            await _handle_oauth_consent_screen(page, username)
        else:
            logger.debug("No consent screen for %s (URL: %s)", username, page.url)

        # Wait for callback server to receive the auth code
        # Browser will be redirected to localhost:8081 which will capture the code
        logger.info(
            "Waiting for callback server to receive auth code for %s...", username
        )
        timeout_seconds = 30
        start_time = time.time()
        while state not in auth_states:
            if time.time() - start_time > timeout_seconds:
                # Take screenshot for debugging
                screenshot_path = f"/tmp/playwright_oauth_timeout_{username}.png"
                await page.screenshot(path=screenshot_path)
                logger.error("Screenshot saved to %s", screenshot_path)
                raise TimeoutError(
                    f"Timeout waiting for OAuth callback for {username} (state={state[:16]}...)"
                )
            await anyio.sleep(0.5)

        auth_code = auth_states[state]
        logger.info("Got auth code for %s: %s...", username, auth_code[:20])

    finally:
        await context.close()

    # Exchange code for token
    logger.info("Exchanging auth code for access token (%s)...", username)
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
        access_token = token_data.get("access_token")

        if not access_token:
            raise ValueError(f"No access_token for {username}: {token_data}")

        logger.info("Successfully obtained OAuth token for %s", username)
        return access_token


# Parallel token retrieval fixture - fetches all OAuth tokens concurrently
@pytest.fixture(scope="session")
async def all_oauth_tokens(
    anyio_backend,
    browser,
    shared_oauth_client_credentials,
    test_users_setup,
    oauth_callback_server,
) -> dict[str, str]:
    """
    Fetch OAuth tokens for all test users in parallel for speed.

    Returns a dict mapping username to OAuth access token.
    This is significantly faster than fetching tokens sequentially.

    Now uses the real callback server with state parameters for reliable
    concurrent token acquisition without race conditions.
    """

    # Get auth_states dict from callback server
    auth_states, callback_url = oauth_callback_server

    start_time = time.time()
    logger.info("Fetching OAuth tokens for all users in parallel...")
    logger.info(
        "Using callback server at %s with state-based correlation", callback_url
    )

    async def get_token_with_delay(username: str, config: dict, delay: float):
        """Get token for a user after a small delay to stagger requests."""
        if delay > 0:
            await anyio.sleep(delay)
        return await _get_oauth_token_for_user(
            browser,
            shared_oauth_client_credentials,
            auth_states,
            username,
            config["password"],
        )

    # Create tasks for all users with staggered starts (0.5s apart)
    user_list = list(test_users_setup.items())
    tokens = {}

    # Run all token fetches concurrently using anyio task groups
    async with anyio.create_task_group() as tg:
        # Create a dict to store results as they complete
        results = {}

        def create_task_wrapper(username: str, config: dict, idx: int):
            async def task():
                try:
                    token = await get_token_with_delay(username, config, idx * 0.5)
                    results[username] = token
                except Exception as e:
                    results[username] = e

            return task

        for idx, (username, config) in enumerate(user_list):
            tg.start_soon(create_task_wrapper(username, config, idx))

    # Build token dict, handling any errors
    for username in results:
        result = results[username]
        if isinstance(result, Exception):
            logger.error("Failed to get OAuth token for %s: %s", username, result)
            raise result
        tokens[username] = result

    elapsed = time.time() - start_time
    logger.info(
        "Successfully fetched %s OAuth tokens in parallel in %ss (~%ss per user)",
        len(tokens),
        format(elapsed, ".1f"),
        format(elapsed / len(tokens), ".1f"),
    )
    return tokens


# Session-scoped OAuth token fixtures - now use the parallel fixture
@pytest.fixture(scope="session")
async def alice_oauth_token(anyio_backend, all_oauth_tokens) -> str:
    """OAuth token for alice (cached for session). Uses shared OAuth client."""
    return all_oauth_tokens["alice"]


@pytest.fixture(scope="session")
async def bob_oauth_token(anyio_backend, all_oauth_tokens) -> str:
    """OAuth token for bob (cached for session). Uses shared OAuth client."""
    return all_oauth_tokens["bob"]


@pytest.fixture(scope="session")
async def charlie_oauth_token(anyio_backend, all_oauth_tokens) -> str:
    """OAuth token for charlie (cached for session). Uses shared OAuth client."""
    return all_oauth_tokens["charlie"]


@pytest.fixture(scope="session")
async def diana_oauth_token(anyio_backend, all_oauth_tokens) -> str:
    """OAuth token for diana (cached for session). Uses shared OAuth client."""
    return all_oauth_tokens["diana"]


@pytest.fixture(scope="session")
async def alice_mcp_client(
    anyio_backend,
    alice_oauth_token: str,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated as alice (owner role)."""
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=alice_oauth_token,
        client_name="Alice MCP",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def bob_mcp_client(
    anyio_backend, bob_oauth_token: str
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated as bob (viewer role)."""
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=bob_oauth_token,
        client_name="Bob MCP",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def charlie_mcp_client(
    anyio_backend,
    charlie_oauth_token: str,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated as charlie (editor role, in 'editors' group)."""
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=charlie_oauth_token,
        client_name="Charlie MCP",
    ) as session:
        yield session


@pytest.fixture(scope="session")
async def diana_mcp_client(
    anyio_backend,
    diana_oauth_token: str,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated as diana (no-access role)."""
    async with create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=diana_oauth_token,
        client_name="Diana MCP",
    ) as session:
        yield session


# Test user/group fixtures for clean test isolation
@pytest.fixture
async def test_user(nc_client: NextcloudClient):
    """
    Fixture that creates a test user and cleans it up after the test.

    Returns a dict with user details that can be customized.
    Usage:
        async def test_something(test_user):
            user_config = test_user
            await nc_client.users.create_user(**user_config)
    """

    # Generate unique user ID to avoid conflicts
    userid = f"testuser_{uuid.uuid4().hex[:8]}"
    password = "SecureTestPassword123!"

    user_config = {
        "userid": userid,
        "password": password,
        "display_name": f"Test User {userid}",
        "email": f"{userid}@example.com",
    }

    # Cleanup before (in case of previous failed run)
    try:
        await nc_client.users.delete_user(userid)
    except Exception:
        pass

    yield user_config

    # Cleanup after test
    try:
        await nc_client.users.delete_user(userid)
        logger.debug("Cleaned up test user: %s", userid)
    except Exception as e:
        logger.warning("Failed to cleanup test user %s: %s", userid, e)


@pytest.fixture
async def test_group(nc_client: NextcloudClient):
    """
    Fixture that creates a test group and cleans it up after the test.

    Returns the group ID.
    """

    # Generate unique group ID to avoid conflicts
    groupid = f"testgroup_{uuid.uuid4().hex[:8]}"

    # Cleanup before (in case of previous failed run)
    try:
        await nc_client.groups.delete_group(groupid)
    except Exception:
        pass

    # Create the group
    await nc_client.groups.create_group(groupid)
    logger.debug("Created test group: %s", groupid)

    yield groupid

    # Cleanup after test
    try:
        await nc_client.groups.delete_group(groupid)
        logger.debug("Cleaned up test group: %s", groupid)
    except Exception as e:
        logger.warning("Failed to cleanup test group %s: %s", groupid, e)


@pytest.fixture
async def test_user_in_group(nc_client: NextcloudClient, test_user, test_group):
    """
    Fixture that creates a test user and adds them to a test group.

    Returns a tuple of (user_config, groupid).
    """
    user_config = test_user
    groupid = test_group

    # Create the user
    await nc_client.users.create_user(**user_config)

    # Add user to group
    await nc_client.users.add_user_to_group(user_config["userid"], groupid)
    logger.debug("Added user %s to group %s", user_config["userid"], groupid)

    yield (user_config, groupid)


# ===========================================================================================
# Astrolabe Dynamic Configuration Fixtures
# ===========================================================================================


@pytest.fixture(scope="session")
async def configure_astrolabe_for_mcp_server(nc_client):
    """Configure Astrolabe app to connect to a specific MCP server.

    This fixture dynamically configures the Astrolabe app's MCP server settings
    and OAuth client, allowing tests to verify integration with different MCP
    server deployments (mcp-oauth, mcp-keycloak, mcp-multi-user-basic, etc.).

    Usage:
        async def test_my_integration(configure_astrolabe_for_mcp_server):
            await configure_astrolabe_for_mcp_server(
                mcp_server_internal_url="http://mcp-oauth:8001",
                mcp_server_public_url="http://localhost:8001"
            )
            # ... test Astrolabe integration ...

    Args:
        nc_client: NextcloudClient fixture for occ command execution

    Returns:
        Async function that accepts:
            - mcp_server_internal_url: Internal Docker URL for PHP app to call MCP APIs
            - mcp_server_public_url: Public URL for OAuth token audience validation
            - client_id: Optional OAuth client ID (default: "nextcloudMcpServerUIPublicClient")
    """

    async def _configure(
        mcp_server_internal_url: str,
        mcp_server_public_url: str,
        client_id: str = "nextcloudMcpServerUIPublicClient",
    ) -> dict[str, str]:
        """Configure Astrolabe for the specified MCP server.

        Returns:
            Dict with client_id and client_secret
        """
        logger.info(
            "Configuring Astrolabe for MCP server: %s (public: %s)",
            mcp_server_internal_url,
            mcp_server_public_url,
        )

        # Configure MCP server URLs in Nextcloud system config
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "app",
                "php",
                "/var/www/html/occ",
                "config:system:set",
                "mcp_server_url",
                "--value",
                mcp_server_internal_url,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to configure MCP server URL. "
                f"Command failed with code {result.returncode}. "
                f"stderr: {result.stderr}, stdout: {result.stdout}"
            )

        # Verify mcp_server_url was actually set
        verify_result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "app",
                "php",
                "/var/www/html/occ",
                "config:system:get",
                "mcp_server_url",
            ],
            capture_output=True,
            text=True,
        )

        actual_url = verify_result.stdout.strip()
        if actual_url != mcp_server_internal_url:
            raise RuntimeError(
                f"MCP server URL verification failed. "
                f"Expected: {mcp_server_internal_url}, Got: {actual_url}"
            )

        logger.info("✓ MCP server URL configured and verified: %s", actual_url)

        # Configure public URL
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "app",
                "php",
                "/var/www/html/occ",
                "config:system:set",
                "mcp_server_public_url",
                "--value",
                mcp_server_public_url,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to configure MCP server public URL. "
                f"Command failed with code {result.returncode}. "
                f"stderr: {result.stderr}, stdout: {result.stdout}"
            )

        logger.info("✓ MCP server public URL configured: %s", mcp_server_public_url)

        # Remove existing OAuth client if it exists
        try:
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
                check=False,  # Don't fail if client doesn't exist
                capture_output=True,
            )
            logger.info("Removed existing OAuth client: %s", client_id)
        except Exception:
            pass

        # Create OAuth client for Astrolabe
        redirect_uri = "http://localhost:8080/apps/astrolabe/oauth/callback"

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
                "Astrolabe",
                redirect_uri,
                "--client_id",
                client_id,
                "--type",
                "confidential",
                "--flow",
                "code",
                "--token_type",
                "jwt",
                "--resource_url",
                mcp_server_public_url,
                "--allowed_scopes",
                "openid profile email offline_access notes.read notes.write calendar.read calendar.write contacts.read contacts.write cookbook.read cookbook.write deck.read deck.write tables.read tables.write files.read files.write",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        # Parse client_secret from JSON output
        client_output = json.loads(result.stdout.strip())
        client_secret = client_output.get("client_secret")

        if not client_secret:
            raise ValueError(
                "Failed to extract client_secret from OAuth client creation"
            )

        logger.info("✓ OAuth client created: %s", client_id)

        # Store client credentials in Nextcloud system config
        subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "app",
                "php",
                "/var/www/html/occ",
                "config:system:set",
                "astrolabe_client_id",
                "--value",
                client_id,
            ],
            check=True,
            capture_output=True,
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
                "config:system:set",
                "astrolabe_client_secret",
                "--value",
                client_secret,
            ],
            check=True,
            capture_output=True,
        )

        logger.info("✓ Client credentials stored in system config")
        logger.info("Astrolabe configured for MCP server: %s", mcp_server_public_url)

        return {"client_id": client_id, "client_secret": client_secret}

    return _configure
