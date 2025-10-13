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


async def get_oauth_token(nextcloud_url: str, username: str, password: str) -> str:
    """
    Get an OAuth access token from Nextcloud OIDC using Client Credentials flow.

    This is a helper function for testing only - it bypasses the normal OAuth flow
    to directly obtain a token for automated testing.

    Args:
        nextcloud_url: Nextcloud base URL
        username: Nextcloud username
        password: Nextcloud password

    Returns:
        Access token string

    Raises:
        Exception: If token acquisition fails
    """
    from nextcloud_mcp_server.auth.client_registration import load_or_register_client

    logger.info(f"Getting OAuth token for testing from {nextcloud_url}")

    # Perform OIDC discovery
    async with httpx.AsyncClient() as http_client:
        discovery_url = f"{nextcloud_url}/.well-known/openid-configuration"
        logger.debug(f"Fetching OIDC discovery from: {discovery_url}")

        discovery_response = await http_client.get(discovery_url)
        if discovery_response.status_code != 200:
            raise Exception(f"OIDC discovery failed: {discovery_response.status_code}")

        oidc_config = discovery_response.json()
        token_endpoint = oidc_config.get("token_endpoint")
        registration_endpoint = oidc_config.get("registration_endpoint")

        if not token_endpoint or not registration_endpoint:
            raise Exception("OIDC discovery missing required endpoints")

        logger.debug(f"Token endpoint: {token_endpoint}")
        logger.debug(f"Registration endpoint: {registration_endpoint}")

        # Get or register an OAuth client
        client_info = await load_or_register_client(
            nextcloud_url=nextcloud_url,
            registration_endpoint=registration_endpoint,
            storage_path=".nextcloud_oauth_test_client.json",
            redirect_uris=["http://localhost:8000/oauth/callback"],
        )

        # Use client credentials to get a token via client_credentials grant
        token_response = await http_client.post(
            token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": client_info.client_id,
                "client_secret": client_info.client_secret,
                "scope": "openid profile email",
            },
        )

        if token_response.status_code != 200:
            logger.error(f"Failed to get OAuth token: {token_response.text}")
            raise Exception(f"Token request failed: {token_response.status_code}")

        token_data = token_response.json()
        access_token = token_data.get("access_token")

        if not access_token:
            raise Exception("No access_token in response")

        logger.info("Successfully obtained OAuth access token for testing")
        return access_token


@pytest.fixture(scope="session")
async def oauth_token() -> str:
    """
    Fixture to obtain an OAuth access token for integration tests.

    This uses the Resource Owner Password flow to get a token without
    requiring interactive browser authentication.
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")
    password = os.getenv("NEXTCLOUD_PASSWORD")

    if not all([nextcloud_host, username, password]):
        pytest.skip(
            "OAuth token fixture requires NEXTCLOUD_HOST, USERNAME, and PASSWORD"
        )

    # Wait for Nextcloud to be ready
    if not await wait_for_nextcloud(nextcloud_host):
        pytest.fail(f"Nextcloud server at {nextcloud_host} is not ready")

    try:
        token = await get_oauth_token(nextcloud_host, username, password)
        return token
    except Exception as e:
        logger.error(f"Failed to obtain OAuth token: {e}")
        pytest.skip(f"Could not obtain OAuth token for testing: {e}")


@pytest.fixture(scope="session")
async def nc_oauth_client(
    interactive_oauth_token: str,
) -> AsyncGenerator[NextcloudClient, Any]:
    """
    Fixture to create a NextcloudClient instance using OAuth authentication.
    Uses the oauth_token fixture to get an access token.
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")

    if not all([nextcloud_host, username]):
        pytest.skip("OAuth client fixture requires NEXTCLOUD_HOST and USERNAME")

    logger.info(f"Creating OAuth NextcloudClient for user: {username}")
    client = NextcloudClient.from_token(
        base_url=nextcloud_host,
        token=interactive_oauth_token,
        username=username,
    )

    # Verify the OAuth client works
    try:
        await client.capabilities()
        logger.info("OAuth NextcloudClient initialized and capabilities checked.")
        yield client
    except Exception as e:
        logger.error(f"Failed to initialize OAuth NextcloudClient: {e}")
        pytest.fail(f"Failed to connect to Nextcloud with OAuth token: {e}")
    finally:
        await client.close()


@pytest.fixture(scope="session")
async def interactive_oauth_token() -> str:
    """
    Fixture to obtain an OAuth access token for integration tests.

    This uses the interactive OAuth flow to get a token.
    """

    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from urllib.parse import urlparse, parse_qs
    import time

    # Use a mutable container to share state across threads
    auth_state = {"code": None}
    httpd = None
    server_thread = None

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Suppress default HTTP logging
            pass

        def do_GET(self):
            if self.path.startswith("/shutdown"):
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>Server shutting down...</h1></body></html>"
                )
                threading.Thread(target=httpd.shutdown).start()
                return

            parsed_path = urlparse(self.path)
            query = parse_qs(parsed_path.query)
            code = query.get("code", [None])[0]
            auth_state["code"] = code
            logger.info(
                f"OAuth callback received. Code: {code[:20] if code else 'None'}..."
            )
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Authentication successful!</h1><p>You can close this window.</p></body></html>"
            )

    httpd = HTTPServer(("localhost", 8081), OAuthCallbackHandler)
    server_thread = threading.Thread(target=httpd.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    from nextcloud_mcp_server.auth.client_registration import load_or_register_client

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
            redirect_uris=["http://localhost:8081"],
            force_register=True,
        )

        # First, open Nextcloud login page to establish session
        login_url = f"{nextcloud_host}/login"
        logger.info(f"Please log in to Nextcloud at: {login_url}")
        logger.info(
            "After logging in, the OAuth authorization will proceed automatically"
        )

        # Construct authorization URL
        auth_url = f"{authorization_endpoint}?response_type=code&client_id={client_info.client_id}&redirect_uri=http://localhost:8081&scope=openid%20profile%20email"

        # Open login page first, then auth URL
        # webbrowser.open(login_url)
        # time.sleep(2)  # Give browser time to load login page
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
                "redirect_uri": "http://localhost:8081",
                "client_id": client_info.client_id,
                "client_secret": client_info.client_secret,
            },
        )

        logger.debug(f"Token response: {token_response.text}")
        token_data = token_response.json()
        logger.debug(f"Token data: {token_data}")
        access_token = token_data.get("access_token")

        # Shut down the server

        await http_client.get("http://localhost:8081/shutdown")
        if httpd:
            httpd.server_close()
        if server_thread:
            server_thread.join(timeout=1)
        return access_token
