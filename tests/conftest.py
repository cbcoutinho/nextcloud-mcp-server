import asyncio
import logging
import os
import uuid
from typing import Any, AsyncGenerator

import httpx
import pytest
from httpx import HTTPStatusError
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)


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
    logger.info(f"Waiting for Nextcloud server at {host} to be ready...")

    async with httpx.AsyncClient(timeout=5.0) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                # Try to hit the status endpoint
                response = await client.get(f"{host}/status.php")
                if response.status_code == 200:
                    data = response.json()
                    if data.get("installed"):
                        logger.info(
                            f"Nextcloud server is ready (version: {data.get('versionstring', 'unknown')})"
                        )
                        return True
            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.debug(f"Attempt {attempt}/{max_attempts}: {e}")

            if attempt < max_attempts:
                logger.info(
                    f"Nextcloud not ready yet, waiting {delay}s... (attempt {attempt}/{max_attempts})"
                )
                await asyncio.sleep(delay)

    logger.error(
        f"Nextcloud server at {host} did not become ready after {max_attempts} attempts"
    )
    return False


@pytest.fixture(scope="session")
async def nc_client() -> AsyncGenerator[NextcloudClient, Any]:
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
        logger.error(f"Failed to initialize NextcloudClient session fixture: {e}")
        pytest.fail(f"Failed to connect to Nextcloud or get capabilities: {e}")
    finally:
        await client.close()


@pytest.fixture(scope="session")
async def nc_mcp_client() -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session for integration tests using streamable-http.
    """
    logger.info("Creating Streamable HTTP client")
    streamable_context = streamablehttp_client("http://127.0.0.1:8000/mcp")
    session_context = None

    try:
        read_stream, write_stream, _ = await streamable_context.__aenter__()
        session_context = ClientSession(read_stream, write_stream)
        session = await session_context.__aenter__()
        await session.initialize()
        logger.info("MCP client session initialized successfully")

        yield session

    finally:
        # Clean up in reverse order, ignoring task scope issues
        if session_context is not None:
            try:
                await session_context.__aexit__(None, None, None)
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug(f"Ignoring cancel scope teardown issue: {e}")
                else:
                    logger.warning(f"Error closing session: {e}")
            except Exception as e:
                logger.warning(f"Error closing session: {e}")

        try:
            await streamable_context.__aexit__(None, None, None)
        except RuntimeError as e:
            if "cancel scope" in str(e):
                logger.debug(f"Ignoring cancel scope teardown issue: {e}")
            else:
                logger.warning(f"Error closing streamable HTTP client: {e}")
        except Exception as e:
            logger.warning(f"Error closing streamable HTTP client: {e}")


@pytest.fixture(scope="session")
async def nc_mcp_oauth_client_interactive(
    interactive_oauth_token: str,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session for OAuth integration tests using interactive authentication.
    Connects to the OAuth-enabled MCP server on port 8001 with OAuth authentication.
    Requires manual browser login.

    For automated testing, use nc_mcp_oauth_client fixture instead.

    Automatically skips when running in GitHub Actions CI.
    """

    logger.info("Creating Streamable HTTP client for OAuth MCP server (Interactive)")

    # Pass OAuth token as Bearer token in headers
    headers = {"Authorization": f"Bearer {interactive_oauth_token}"}
    streamable_context = streamablehttp_client(
        "http://127.0.0.1:8001/mcp", headers=headers
    )
    session_context = None

    try:
        read_stream, write_stream, _ = await streamable_context.__aenter__()
        session_context = ClientSession(read_stream, write_stream)
        session = await session_context.__aenter__()
        await session.initialize()
        logger.info("OAuth MCP client session (Interactive) initialized successfully")

        yield session

    finally:
        # Clean up in reverse order, ignoring task scope issues
        if session_context is not None:
            try:
                await session_context.__aexit__(None, None, None)
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug(f"Ignoring cancel scope teardown issue: {e}")
                else:
                    logger.warning(f"Error closing OAuth session (Interactive): {e}")
            except Exception as e:
                logger.warning(f"Error closing OAuth session (Interactive): {e}")

        try:
            await streamable_context.__aexit__(None, None, None)
        except RuntimeError as e:
            if "cancel scope" in str(e):
                logger.debug(f"Ignoring cancel scope teardown issue: {e}")
            else:
                logger.warning(
                    f"Error closing OAuth streamable HTTP client (Interactive): {e}"
                )
        except Exception as e:
            logger.warning(
                f"Error closing OAuth streamable HTTP client (Interactive): {e}"
            )


@pytest.fixture(scope="session")
async def nc_mcp_oauth_client(
    playwright_oauth_token: str,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session for OAuth integration tests using Playwright automation.
    Connects to the OAuth-enabled MCP server on port 8001 with OAuth authentication.

    This is the default OAuth MCP fixture using headless browser automation suitable for CI/CD.
    For interactive testing with manual browser login, use nc_mcp_oauth_client_interactive instead.
    """
    logger.info("Creating Streamable HTTP client for OAuth MCP server (Playwright)")

    # Pass OAuth token as Bearer token in headers
    headers = {"Authorization": f"Bearer {playwright_oauth_token}"}
    streamable_context = streamablehttp_client(
        "http://127.0.0.1:8001/mcp", headers=headers
    )
    session_context = None

    try:
        read_stream, write_stream, _ = await streamable_context.__aenter__()
        session_context = ClientSession(read_stream, write_stream)
        session = await session_context.__aenter__()
        await session.initialize()
        logger.info("OAuth MCP client session (Playwright) initialized successfully")

        yield session

    finally:
        # Clean up in reverse order, ignoring task scope issues
        if session_context is not None:
            try:
                await session_context.__aexit__(None, None, None)
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug(f"Ignoring cancel scope teardown issue: {e}")
                else:
                    logger.warning(f"Error closing Playwright OAuth session: {e}")
            except Exception as e:
                logger.warning(f"Error closing Playwright OAuth session: {e}")

        try:
            await streamable_context.__aexit__(None, None, None)
        except RuntimeError as e:
            if "cancel scope" in str(e):
                logger.debug(f"Ignoring cancel scope teardown issue: {e}")
            else:
                logger.warning(
                    f"Error closing Playwright OAuth streamable HTTP client: {e}"
                )
        except Exception as e:
            logger.warning(
                f"Error closing Playwright OAuth streamable HTTP client: {e}"
            )


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

    logger.info(f"Creating temporary note: {note_title}")
    try:
        created_note_data = await nc_client.notes.create_note(
            title=note_title, content=note_content, category=note_category
        )
        note_id = created_note_data.get("id")
        if not note_id:
            pytest.fail("Failed to get ID from created temporary note.")

        logger.info(f"Temporary note created with ID: {note_id}")
        yield created_note_data  # Provide the created note data to the test

    finally:
        if note_id:
            logger.info(f"Cleaning up temporary note ID: {note_id}")
            try:
                await nc_client.notes.delete_note(note_id=note_id)
                logger.info(f"Successfully deleted temporary note ID: {note_id}")
            except HTTPStatusError as e:
                # Ignore 404 if note was already deleted by the test itself
                if e.response.status_code != 404:
                    logger.error(f"HTTP error deleting temporary note {note_id}: {e}")
                else:
                    logger.warning(f"Temporary note {note_id} already deleted (404).")
            except Exception as e:
                logger.error(f"Unexpected error deleting temporary note {note_id}: {e}")


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
        f"Adding attachment '{attachment_filename}' to temporary note ID: {note_id} (category: '{note_category or ''}')"
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
        logger.info(f"Attachment '{attachment_filename}' added successfully.")

        yield note_data, attachment_filename, attachment_content

        # Cleanup for the attachment is handled by the notes_delete_note call
        # in the temporary_note fixture's finally block (which deletes the .attachments dir)

    except Exception as e:
        logger.error(f"Failed to add attachment in fixture: {e}")
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
    logger.info(f"Creating temporary addressbook: {addressbook_name}")
    try:
        await nc_client.contacts.create_addressbook(
            name=addressbook_name, display_name=f"Test Addressbook {addressbook_name}"
        )
        logger.info(f"Temporary addressbook created: {addressbook_name}")
        yield addressbook_name
    finally:
        logger.info(f"Cleaning up temporary addressbook: {addressbook_name}")
        try:
            await nc_client.contacts.delete_addressbook(name=addressbook_name)
            logger.info(
                f"Successfully deleted temporary addressbook: {addressbook_name}"
            )
        except HTTPStatusError as e:
            if e.response.status_code != 404:
                logger.error(
                    f"HTTP error deleting temporary addressbook {addressbook_name}: {e}"
                )
            else:
                logger.warning(
                    f"Temporary addressbook {addressbook_name} already deleted (404)."
                )
        except Exception as e:
            logger.error(
                f"Unexpected error deleting temporary addressbook {addressbook_name}: {e}"
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
    logger.info(f"Creating temporary contact in addressbook: {addressbook_name}")
    try:
        await nc_client.contacts.create_contact(
            addressbook=addressbook_name,
            uid=contact_uid,
            contact_data=contact_data,
        )
        logger.info(f"Temporary contact created with UID: {contact_uid}")
        yield contact_uid
    finally:
        logger.info(f"Cleaning up temporary contact: {contact_uid}")
        try:
            await nc_client.contacts.delete_contact(
                addressbook=addressbook_name, uid=contact_uid
            )
            logger.info(f"Successfully deleted temporary contact: {contact_uid}")
        except HTTPStatusError as e:
            if e.response.status_code != 404:
                logger.error(
                    f"HTTP error deleting temporary contact {contact_uid}: {e}"
                )
            else:
                logger.warning(
                    f"Temporary contact {contact_uid} already deleted (404)."
                )
        except Exception as e:
            logger.error(
                f"Unexpected error deleting temporary contact {contact_uid}: {e}"
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

    logger.info(f"Creating temporary deck board: {board_title}")
    try:
        created_board = await nc_client.deck.create_board(board_title, board_color)
        board_id = created_board.id
        created_board_data = {
            "id": board_id,
            "title": created_board.title,
            "color": created_board.color,
            "archived": getattr(created_board, "archived", False),
        }

        logger.info(f"Temporary board created with ID: {board_id}")
        yield created_board_data

    finally:
        if board_id:
            logger.info(f"Cleaning up temporary board ID: {board_id}")
            try:
                await nc_client.deck.delete_board(board_id)
                logger.info(f"Successfully deleted temporary board ID: {board_id}")
            except HTTPStatusError as e:
                # Ignore 404 if board was already deleted by the test itself
                if e.response.status_code not in [404, 403]:
                    logger.error(f"HTTP error deleting temporary board {board_id}: {e}")
                else:
                    logger.warning(
                        f"Temporary board {board_id} already deleted or access denied ({e.response.status_code})."
                    )
            except Exception as e:
                logger.error(
                    f"Unexpected error deleting temporary board {board_id}: {e}"
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

    logger.info(f"Creating temporary stack in board ID: {board_id}")
    try:
        stack = await nc_client.deck.create_stack(board_id, stack_title, stack_order)
        stack_data = {
            "id": stack.id,
            "title": stack.title,
            "order": stack.order,
            "boardId": board_id,
        }

        logger.info(f"Temporary stack created with ID: {stack.id}")
        yield (board_data, stack_data)

    finally:
        # Clean up - delete stack
        if stack and hasattr(stack, "id"):
            logger.info(f"Cleaning up temporary stack ID: {stack.id}")
            try:
                await nc_client.deck.delete_stack(board_id, stack.id)
                logger.info(f"Successfully deleted temporary stack ID: {stack.id}")
            except HTTPStatusError as e:
                if e.response.status_code not in [404, 403]:
                    logger.error(f"HTTP error deleting temporary stack {stack.id}: {e}")
                else:
                    logger.warning(
                        f"Temporary stack {stack.id} already deleted or access denied ({e.response.status_code})."
                    )
            except Exception as e:
                logger.error(
                    f"Unexpected error deleting temporary stack {stack.id}: {e}"
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
        f"Creating temporary card in stack ID: {stack_id}, board ID: {board_id}"
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

        logger.info(f"Temporary card created with ID: {card.id}")
        yield (board_data, stack_data, card_data)

    finally:
        # Clean up - delete card
        if card and hasattr(card, "id"):
            logger.info(f"Cleaning up temporary card ID: {card.id}")
            try:
                await nc_client.deck.delete_card(board_id, stack_id, card.id)
                logger.info(f"Successfully deleted temporary card ID: {card.id}")
            except HTTPStatusError as e:
                if e.response.status_code not in [404, 403]:
                    logger.error(f"HTTP error deleting temporary card {card.id}: {e}")
                else:
                    logger.warning(
                        f"Temporary card {card.id} already deleted or access denied ({e.response.status_code})."
                    )
            except Exception as e:
                logger.error(f"Unexpected error deleting temporary card {card.id}: {e}")


@pytest.fixture(scope="session")
async def nc_oauth_client_interactive(
    interactive_oauth_token: str,
) -> AsyncGenerator[NextcloudClient, Any]:
    """
    Fixture to create a NextcloudClient instance using interactive OAuth authentication.
    Uses the interactive_oauth_token fixture which requires manual browser login.

    For automated testing, use nc_oauth_client fixture instead.

    Automatically skips when running in GitHub Actions CI.
    """

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")

    if not all([nextcloud_host, username]):
        pytest.skip("OAuth client fixture requires NEXTCLOUD_HOST and USERNAME")

    logger.info(f"Creating OAuth NextcloudClient (Interactive) for user: {username}")
    client = NextcloudClient.from_token(
        base_url=nextcloud_host,
        token=interactive_oauth_token,
        username=username,
    )

    # Verify the OAuth client works
    try:
        await client.capabilities()
        logger.info(
            "OAuth NextcloudClient (Interactive) initialized and capabilities checked."
        )
        yield client
    except Exception as e:
        logger.error(f"Failed to initialize OAuth NextcloudClient (Interactive): {e}")
        pytest.fail(f"Failed to connect to Nextcloud with OAuth token: {e}")
    finally:
        await client.close()


@pytest.fixture(scope="session")
async def nc_oauth_client(
    playwright_oauth_token: str,
) -> AsyncGenerator[NextcloudClient, Any]:
    """
    Fixture to create a NextcloudClient instance using automated Playwright OAuth authentication.
    This is the default OAuth fixture using headless browser automation suitable for CI/CD.

    For interactive testing with manual browser login, use nc_oauth_client_interactive instead.
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")

    if not all([nextcloud_host, username]):
        pytest.skip("OAuth client fixture requires NEXTCLOUD_HOST and USERNAME")

    logger.info(f"Creating OAuth NextcloudClient (Playwright) for user: {username}")
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
        logger.error(f"Failed to initialize OAuth NextcloudClient (Playwright): {e}")
        pytest.fail(f"Failed to connect to Nextcloud with Playwright OAuth token: {e}")
    finally:
        await client.close()


@pytest.fixture(scope="session")
def oauth_callback_server():
    """
    Fixture to create an HTTP server for OAuth callback handling.

    Yields a tuple of (auth_state, server_url) where:
    - auth_state: A dict with {"code": None} that will be populated with the auth code
    - server_url: The callback URL for the server (e.g., "http://localhost:8081")

    The server automatically shuts down when the fixture is torn down.

    Automatically skips when running in GitHub Actions CI.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    # Use a mutable container to share state across threads
    auth_state = {"code": None}
    httpd = None
    server_thread = None

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Suppress default HTTP logging
            pass

        def do_GET(self):
            # Ignore subsequent requests if we already have a code
            # (this is a session-scoped fixture, so only process the first auth code)
            if auth_state["code"] is not None:
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>Authentication already completed</h1></body></html>"
                )
                return

            # Parse the callback request
            parsed_path = urlparse(self.path)
            query = parse_qs(parsed_path.query)
            code = query.get("code", [None])[0]

            # Only process if we have a valid code
            if code:
                auth_state["code"] = code
                logger.info(f"OAuth callback received. Code: {code[:20]}...")
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>Authentication successful!</h1><p>You can close this window.</p></body></html>"
                )
            else:
                # Ignore requests without a code (e.g., favicon requests)
                logger.debug(f"Ignoring request without auth code: {self.path}")
                self.send_response(404)
                self.end_headers()

    try:
        # Start the HTTP server
        httpd = HTTPServer(("localhost", 8081), OAuthCallbackHandler)
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        logger.info("OAuth callback server started on http://localhost:8081")

        # Yield the auth state and server URL
        yield auth_state, "http://localhost:8081"

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
async def interactive_oauth_token(oauth_callback_server) -> str:
    """
    Fixture to obtain an OAuth access token for integration tests.

    This uses the interactive OAuth flow to get a token.
    Depends on oauth_callback_server fixture for HTTP callback handling.

    Automatically skips when running in GitHub Actions CI.
    """

    # Skip if GITHUB_ACTIONS env var available, meaning that no interactive
    # browser is available
    if "GITHUB_ACTIONS" in os.environ:
        pytest.skip(
            reason="Running in GitHub Action, skipping due to lack of interactive browser"
        )

    import time
    import webbrowser

    from nextcloud_mcp_server.auth.client_registration import load_or_register_client

    # Unpack the server fixture
    auth_state, callback_url = oauth_callback_server

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    async with httpx.AsyncClient() as http_client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await http_client.get(discovery_url)
        oidc_config = discovery_response.json()
        token_endpoint = oidc_config.get("token_endpoint")
        registration_endpoint = oidc_config.get("registration_endpoint")
        authorization_endpoint = oidc_config.get("authorization_endpoint")
        client_info = await load_or_register_client(
            nextcloud_url=nextcloud_host,
            registration_endpoint=registration_endpoint,
            storage_path=".nextcloud_oauth_test_client.json",
            redirect_uris=[callback_url],
            force_register=True,
        )

        # First, open Nextcloud login page to establish session
        login_url = f"{nextcloud_host}/login"
        logger.info(f"Please log in to Nextcloud at: {login_url}")
        logger.info(
            "After logging in, the OAuth authorization will proceed automatically"
        )

        # Construct authorization URL
        auth_url = f"{authorization_endpoint}?response_type=code&client_id={client_info.client_id}&redirect_uri={callback_url}&scope=openid%20profile%20email"

        # Open authorization URL in browser
        webbrowser.open(auth_url)

        # Wait for auth code with timeout
        timeout = 120  # 2 minutes
        start_time = time.time()
        while not auth_state["code"]:
            if time.time() - start_time > timeout:
                raise TimeoutError("OAuth authorization timed out after 2 minutes")
            logger.info("Waiting for OAuth authorization...")
            time.sleep(1)

        auth_code = auth_state["code"]
        logger.info("Received authorization code, exchanging for token...")

        token_response = await http_client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": callback_url,
                "client_id": client_info.client_id,
                "client_secret": client_info.client_secret,
            },
        )

        logger.debug(f"Token response: {token_response.text}")
        token_data = token_response.json()
        logger.debug(f"Token data: {token_data}")
        access_token = token_data.get("access_token")

        return access_token


@pytest.fixture(scope="session")
async def playwright_oauth_token(browser) -> str:
    """
    Fixture to obtain an OAuth access token using Playwright headless browser automation.

    This fully automates the OAuth flow by:
    1. Discovering OIDC endpoints
    2. Registering an OAuth client
    3. Navigating to authorization URL in headless browser
    4. Programmatically filling in login form
    5. Handling OAuth consent
    6. Extracting auth code from redirect
    7. Exchanging code for access token

    Environment variables required:
    - NEXTCLOUD_HOST: Nextcloud instance URL
    - NEXTCLOUD_USERNAME: Username for login
    - NEXTCLOUD_PASSWORD: Password for login

    Playwright Configuration:
    - Configure browser via pytest CLI args: --browser firefox --headed
    - Browser fixture provided by pytest-playwright-asyncio
    - See: https://playwright.dev/python/docs/test-runners
    """
    from urllib.parse import parse_qs, urlparse

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")
    password = os.getenv("NEXTCLOUD_PASSWORD")

    if not all([nextcloud_host, username, password]):
        pytest.skip(
            "Playwright OAuth requires NEXTCLOUD_HOST, NEXTCLOUD_USERNAME, and NEXTCLOUD_PASSWORD"
        )

    logger.info("Starting Playwright-based OAuth flow...")

    # Use async httpx for all HTTP operations
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        # OIDC Discovery
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        logger.debug(f"Fetching OIDC discovery from: {discovery_url}")

        discovery_response = await http_client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        token_endpoint = oidc_config.get("token_endpoint")
        registration_endpoint = oidc_config.get("registration_endpoint")
        authorization_endpoint = oidc_config.get("authorization_endpoint")

        if not all([token_endpoint, registration_endpoint, authorization_endpoint]):
            raise ValueError("OIDC discovery missing required endpoints")

        logger.debug(f"Authorization endpoint: {authorization_endpoint}")
        logger.debug(f"Token endpoint: {token_endpoint}")

        # Register OAuth client with a callback that won't actually be used
        # (we'll extract the code from the browser URL instead)
        callback_url = "http://localhost:9999/oauth/callback"

        # Register client asynchronously
        client_metadata = {
            "client_name": "Nextcloud MCP Server - Playwright Tests",
            "redirect_uris": [callback_url],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "openid profile email",
        }

        reg_response = await http_client.post(
            registration_endpoint,
            json=client_metadata,
            headers={"Content-Type": "application/json"},
        )
        reg_response.raise_for_status()
        client_info_dict = reg_response.json()

        client_id = client_info_dict["client_id"]
        client_secret = client_info_dict["client_secret"]

        # Construct authorization URL
        auth_url = (
            f"{authorization_endpoint}?"
            f"response_type=code&"
            f"client_id={client_id}&"
            f"redirect_uri={callback_url}&"
            f"scope=openid%20profile%20email"
        )

        logger.info("Opening browser for OAuth authorization...")

        # Async browser automation using pytest-playwright's browser fixture
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        try:
            # Navigate to authorization URL
            logger.debug(f"Navigating to: {auth_url}")
            await page.goto(auth_url, wait_until="networkidle", timeout=30000)

            # Check if we need to login first
            current_url = page.url
            logger.debug(f"Current URL after navigation: {current_url}")

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
                await page.wait_for_load_state("networkidle", timeout=30000)
                current_url = page.url
                logger.info(f"After login, current URL: {current_url}")

            # Now we should be on the OAuth authorization/consent page or already redirected
            # Check if there's an authorize button to click
            try:
                # Look for common authorization button patterns
                authorize_button = await page.query_selector(
                    'button:has-text("Authorize"), button:has-text("Allow"), input[type="submit"][value*="uthoriz"]'
                )

                if authorize_button:
                    logger.info(
                        "Authorization consent page detected, clicking authorize..."
                    )
                    await authorize_button.click()
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    current_url = page.url
                    logger.debug(f"After authorization, current_url: {current_url}")
            except Exception as e:
                logger.debug(
                    f"No authorization button found or already authorized: {e}"
                )

            # Wait for redirect to callback URL (which will fail to load, but we just need the URL)
            try:
                # The redirect might fail since localhost:9999 isn't actually running
                # But we can still extract the code from the URL
                await page.wait_for_url(f"{callback_url}*", timeout=10000)
            except Exception as e:
                # Expected - the callback URL won't load, but we should have the URL
                logger.debug(f"Callback redirect (expected to fail): {e}")

            # Extract auth code from URL
            final_url = page.url
            logger.debug(f"Final URL: {final_url}")

            parsed_url = urlparse(final_url)
            query_params = parse_qs(parsed_url.query)
            auth_code = query_params.get("code", [None])[0]

            if not auth_code:
                # Take a screenshot for debugging
                screenshot_path = "/tmp/playwright_oauth_error.png"
                await page.screenshot(path=screenshot_path)
                logger.error(f"Screenshot saved to {screenshot_path}")
                raise ValueError(
                    f"No authorization code found in redirect URL: {final_url}"
                )

            logger.info(
                f"Successfully extracted authorization code: {auth_code[:20]}..."
            )

        finally:
            await context.close()

        # Exchange authorization code for access token
        logger.info("Exchanging authorization code for access token...")
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


# Alternative fixtures using Playwright token (for automated/CI testing)


@pytest.fixture(scope="session")
async def nc_oauth_client_playwright(
    playwright_oauth_token: str,
) -> AsyncGenerator[NextcloudClient, Any]:
    """
    Fixture to create a NextcloudClient instance using automated Playwright OAuth authentication.
    This fixture uses headless browser automation and is suitable for CI/CD pipelines.

    For interactive testing, use nc_oauth_client fixture instead.
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")

    if not all([nextcloud_host, username]):
        pytest.skip(
            "Playwright OAuth client fixture requires NEXTCLOUD_HOST and USERNAME"
        )

    logger.info(f"Creating OAuth NextcloudClient (Playwright) for user: {username}")
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
        logger.error(f"Failed to initialize Playwright OAuth NextcloudClient: {e}")
        pytest.fail(f"Failed to connect to Nextcloud with Playwright OAuth token: {e}")
    finally:
        await client.close()


@pytest.fixture(scope="session")
async def nc_mcp_oauth_client_playwright(
    playwright_oauth_token: str,
) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session for OAuth integration tests using Playwright automation.
    Connects to the OAuth-enabled MCP server on port 8001 with OAuth authentication.

    This fixture uses headless browser automation and is suitable for CI/CD pipelines.
    For interactive testing, use nc_mcp_oauth_client fixture instead.
    """
    logger.info("Creating Streamable HTTP client for OAuth MCP server (Playwright)")

    # Pass OAuth token as Bearer token in headers
    headers = {"Authorization": f"Bearer {playwright_oauth_token}"}
    streamable_context = streamablehttp_client(
        "http://127.0.0.1:8001/mcp", headers=headers
    )
    session_context = None

    try:
        read_stream, write_stream, _ = await streamable_context.__aenter__()
        session_context = ClientSession(read_stream, write_stream)
        session = await session_context.__aenter__()
        await session.initialize()
        logger.info("OAuth MCP client session (Playwright) initialized successfully")

        yield session

    finally:
        # Clean up in reverse order, ignoring task scope issues
        if session_context is not None:
            try:
                await session_context.__aexit__(None, None, None)
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug(f"Ignoring cancel scope teardown issue: {e}")
                else:
                    logger.warning(f"Error closing Playwright OAuth session: {e}")
            except Exception as e:
                logger.warning(f"Error closing Playwright OAuth session: {e}")

        try:
            await streamable_context.__aexit__(None, None, None)
        except RuntimeError as e:
            if "cancel scope" in str(e):
                logger.debug(f"Ignoring cancel scope teardown issue: {e}")
            else:
                logger.warning(
                    f"Error closing Playwright OAuth streamable HTTP client: {e}"
                )
        except Exception as e:
            logger.warning(
                f"Error closing Playwright OAuth streamable HTTP client: {e}"
            )
