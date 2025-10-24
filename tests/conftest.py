import logging
import os
import uuid
from typing import Any, AsyncGenerator

import anyio
import httpx
import pytest
from httpx import HTTPStatusError
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# Default scopes for OAuth testing - all app-specific read/write scopes
DEFAULT_FULL_SCOPES = (
    "openid profile email "
    "notes:read notes:write "
    "calendar:read calendar:write "
    "todo:read todo:write "
    "contacts:read contacts:write "
    "cookbook:read cookbook:write "
    "deck:read deck:write "
    "tables:read tables:write "
    "files:read files:write "
    "sharing:read sharing:write"
)

# Read-only scopes (all read scopes across apps) - should match DEFAULT_FULL_SCOPES read portion
DEFAULT_READ_SCOPES = (
    "openid profile email "
    "notes:read "
    "calendar:read "
    "todo:read "
    "contacts:read "
    "cookbook:read "
    "deck:read "
    "tables:read "
    "files:read "
    "sharing:read"
)

# Write-only scopes (all write scopes across apps) - should match DEFAULT_FULL_SCOPES write portion
DEFAULT_WRITE_SCOPES = (
    "openid profile email "
    "notes:write "
    "calendar:write "
    "todo:write "
    "contacts:write "
    "cookbook:write "
    "deck:write "
    "tables:write "
    "files:write "
    "sharing:write"
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
                await anyio.sleep(delay)

    logger.error(
        f"Nextcloud server at {host} did not become ready after {max_attempts} attempts"
    )
    return False


async def create_mcp_client_session(
    url: str,
    token: str | None = None,
    client_name: str = "MCP",
) -> AsyncGenerator[ClientSession, Any]:
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

    Yields:
        Initialized MCP ClientSession

    Note:
        This implementation uses native async context managers instead of manually
        calling __aenter__/__aexit__. This ensures that anyio's structured concurrency
        requirements are met, as Python guarantees LIFO cleanup order for nested
        context managers. See: https://github.com/modelcontextprotocol/python-sdk/issues/577
    """
    logger.info(f"Creating Streamable HTTP client for {client_name}")

    # Prepare headers with OAuth token if provided
    headers = {"Authorization": f"Bearer {token}"} if token else None

    # Use native async with - Python ensures LIFO cleanup
    # Cleanup order will be: ClientSession.__aexit__ -> streamablehttp_client.__aexit__
    async with streamablehttp_client(url, headers=headers) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            logger.info(f"{client_name} client session initialized successfully")
            yield session

    # Cleanup happens automatically in LIFO order - no exception suppression needed
    logger.debug(f"{client_name} client session cleaned up successfully")


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
        logger.error(f"Failed to initialize NextcloudClient session fixture: {e}")
        pytest.fail(f"Failed to connect to Nextcloud or get capabilities: {e}")
    finally:
        await client.close()


@pytest.fixture(scope="session")
async def nc_mcp_client(anyio_backend) -> AsyncGenerator[ClientSession, Any]:
    """
    Fixture to create an MCP client session for integration tests using streamable-http.

    Uses anyio pytest plugin for proper async fixture handling.
    """
    async for session in create_mcp_client_session(
        url="http://localhost:8000/mcp", client_name="Basic MCP"
    ):
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
    async for session in create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token,
        client_name="OAuth MCP (Playwright)",
    ):
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
    async for session in create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token_jwt,
        client_name="OAuth JWT MCP (Playwright)",
    ):
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
    async for session in create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token_read_only,
        client_name="OAuth JWT MCP Read-Only (Playwright)",
    ):
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
    async for session in create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token_write_only,
        client_name="OAuth JWT MCP Write-Only (Playwright)",
    ):
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
    async for session in create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token_full_access,
        client_name="OAuth JWT MCP Full Access (Playwright)",
    ):
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
    application-specific scopes (notes:read, notes:write, etc.).

    Expected behavior: Should see 0 tools (all tools require custom scopes).

    Uses JWT tokens because they embed scope information in claims,
    enabling proper scope-based tool filtering.
    """
    async for session in create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=playwright_oauth_token_no_custom_scopes,
        client_name="OAuth JWT MCP No Custom Scopes (Playwright)",
    ):
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
        logger.info(f"Creating shared test calendar: {calendar_name}")
        result = await nc_client.calendar.create_calendar(
            calendar_name=calendar_name,
            display_name=f"Shared Test Calendar {calendar_name}",
            description="Shared calendar for integration testing (reused across tests)",
            color="#FF5722",
        )

        if result["status_code"] not in [200, 201]:
            pytest.skip(f"Failed to create shared test calendar: {result}")

        logger.info(f"Created shared test calendar: {calendar_name}")
        yield calendar_name

    except Exception as e:
        logger.error(f"Error setting up shared test calendar: {e}")
        pytest.skip(f"Shared calendar setup failed: {e}")

    finally:
        # Cleanup: Delete the shared calendar at end of session
        try:
            logger.info(f"Cleaning up shared test calendar: {calendar_name}")
            await nc_client.calendar.delete_calendar(calendar_name)
            logger.info(f"Successfully deleted shared test calendar: {calendar_name}")
        except Exception as e:
            logger.error(f"Error deleting shared test calendar {calendar_name}: {e}")


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
        logger.info(f"Creating second shared test calendar: {calendar_name}")
        result = await nc_client.calendar.create_calendar(
            calendar_name=calendar_name,
            display_name=f"Shared Test Calendar 2 {calendar_name}",
            description="Second shared calendar for cross-calendar testing",
            color="#4CAF50",
        )

        if result["status_code"] not in [200, 201]:
            pytest.skip(f"Failed to create second shared test calendar: {result}")

        logger.info(f"Created second shared test calendar: {calendar_name}")

        # Verify calendar was created by listing calendars
        # Add small delay to allow calendar to propagate in the system

        await anyio.sleep(1.0)  # Allow time for calendar to propagate

        calendars = await nc_client.calendar.list_calendars()
        calendar_names = [cal["name"] for cal in calendars]
        if calendar_name not in calendar_names:
            logger.warning(
                f"Calendar {calendar_name} not found immediately after creation. Available: {calendar_names}"
            )
            # Try one more time after a longer delay
            await anyio.sleep(3)  # Additional wait for calendar synchronization
            calendars = await nc_client.calendar.list_calendars()
            calendar_names = [cal["name"] for cal in calendars]
            if calendar_name not in calendar_names:
                logger.error(
                    f"Calendar {calendar_name} still not found after retries. Available: {calendar_names}"
                )
                pytest.fail(
                    f"Failed to create second shared calendar: {calendar_name} not found in listing"
                )

        logger.info(
            f"Successfully verified second shared test calendar: {calendar_name}"
        )
        yield calendar_name

    except Exception as e:
        logger.error(f"Error setting up second shared test calendar: {e}")
        pytest.skip(f"Second shared calendar setup failed: {e}")

    finally:
        # Cleanup: Delete the second shared calendar at end of session
        try:
            logger.info(f"Cleaning up second shared test calendar: {calendar_name}")
            await nc_client.calendar.delete_calendar(calendar_name)
            logger.info(
                f"Successfully deleted second shared test calendar: {calendar_name}"
            )
        except Exception as e:
            logger.error(
                f"Error deleting second shared test calendar {calendar_name}: {e}"
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
        logger.info(f"Cleaning up todos from shared calendar: {calendar_name}")
        todos = await nc_client.calendar.list_todos(calendar_name)
        for todo in todos:
            try:
                await nc_client.calendar.delete_todo(calendar_name, todo["uid"])
            except Exception as e:
                logger.warning(f"Error deleting todo {todo['uid']}: {e}")
        logger.info(f"Cleaned up {len(todos)} todos from shared calendar")
    except Exception as e:
        logger.error(f"Error cleaning up todos from calendar {calendar_name}: {e}")


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

    Supports multiple concurrent OAuth flows using state parameters for correlation.

    Yields a tuple of (auth_states, server_url) where:
    - auth_states: A dict mapping state parameter to auth code
    - server_url: The callback URL for the server (e.g., "http://localhost:8081")

    The server automatically shuts down when the fixture is torn down.
    """
    # Skip OAuth tests in GitHub Actions - Playwright browser automation
    # has issues with localhost callback server in CI environment
    # if os.getenv("GITHUB_ACTIONS"):
    # pytest.skip(
    # "OAuth tests with browser automation not supported in GitHub Actions CI"
    # )

    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

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
                        f"OAuth callback received for state={state[:16]}... Code: {code[:20]}..."
                    )
                else:
                    # Fallback for flows without state parameter (legacy interactive flow)
                    auth_states["_default"] = code
                    logger.info(
                        f"OAuth callback received (no state). Code: {code[:20]}..."
                    )

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

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Shared OAuth client requires NEXTCLOUD_HOST")

    # Get callback URL from the real callback server
    auth_states, callback_url = oauth_callback_server

    logger.info("Setting up shared OAuth client credentials for all test users...")
    logger.info(f"Using real callback server at: {callback_url}")

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
        # Cache to file to avoid creating new client on every test run
        client_id, client_secret = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Pytest - Shared Test Client (Opaque)",
            allowed_scopes=DEFAULT_FULL_SCOPES,
            token_type="Bearer",  # Opaque tokens for port 8001
            cache_file=".nextcloud_oauth_shared_test_client.json",
        )

        logger.info(f"Shared OAuth client ready: {client_id[:16]}...")
        logger.info(
            "This opaque token client with full scopes will be reused for all test user authentications"
        )

        return (
            client_id,
            client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )


@pytest.fixture(scope="session")
async def shared_jwt_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """
    Fixture to obtain shared JWT OAuth client credentials for testing JWT token behavior.

    Creates a JWT OAuth client with full scopes (all app read/write scopes). The client
    is configured with token_type="JWT" to request JWT-formatted access tokens from the
    OIDC server (instead of opaque tokens).

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Shared JWT OAuth client requires NEXTCLOUD_HOST")

    # Get callback URL from the real callback server
    auth_states, callback_url = oauth_callback_server

    logger.info("Setting up shared JWT OAuth client credentials...")
    logger.info(f"Using real callback server at: {callback_url}")

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
        # Cache to file to avoid creating new client on every test run
        client_id, client_secret = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Pytest - Shared JWT Test Client",
            allowed_scopes=DEFAULT_FULL_SCOPES,
            token_type="JWT",  # Explicitly set JWT token type
            cache_file=".nextcloud_oauth_shared_jwt_test_client.json",
        )

        logger.info(f"Shared JWT OAuth client ready: {client_id[:16]}...")
        logger.info(
            "This JWT client with full scopes will be reused for JWT MCP server tests"
        )

        return (
            client_id,
            client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )


async def _create_oauth_client_with_scopes(
    callback_url: str,
    client_name: str,
    allowed_scopes: str,
    token_type: str = "JWT",
    cache_file: str | None = None,
) -> tuple[str, str]:
    """
    Helper function to create an OAuth client with specific allowed_scopes using DCR.

    Supports optional file-based caching to avoid creating duplicate clients.

    Args:
        callback_url: OAuth callback URL
        client_name: Name of the OAuth client
        allowed_scopes: Space-separated list of allowed scopes
        token_type: Either "JWT" or "Bearer" (default: "JWT")
        cache_file: Optional path to cache file (e.g., ".nextcloud_oauth_shared_test_client.json")

    Returns:
        Tuple of (client_id, client_secret)
    """
    import json
    from pathlib import Path

    from nextcloud_mcp_server.auth.client_registration import register_client

    # Try to load from cache if specified
    if cache_file:
        cache_path = Path(cache_file)
        if cache_path.exists():
            try:
                with open(cache_path, "r") as f:
                    cached_data = json.load(f)

                client_id = cached_data.get("client_id")
                client_secret = cached_data.get("client_secret")

                if client_id and client_secret:
                    logger.info(
                        f"Loaded cached OAuth client from {cache_file}: {client_id[:16]}..."
                    )
                    return client_id, client_secret
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.warning(f"Failed to load cached client from {cache_file}: {e}")

    logger.info(
        f"Creating {token_type} OAuth client '{client_name}' with scopes: {allowed_scopes} using DCR"
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

    client_id = client_info.client_id
    client_secret = client_info.client_secret

    logger.info(
        f"Created OAuth client via DCR: {client_id[:16]}... with scopes: {allowed_scopes}"
    )

    # Save to cache if specified
    if cache_file:
        cache_path = Path(cache_file)
        try:
            # Create parent directory if needed
            cache_path.parent.mkdir(parents=True, exist_ok=True)

            # Save client data
            with open(cache_path, "w") as f:
                json.dump(
                    {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "redirect_uris": [callback_url],
                    },
                    f,
                    indent=2,
                )

            # Set restrictive permissions
            cache_path.chmod(0o600)

            logger.info(f"Cached OAuth client to {cache_file}")
        except OSError as e:
            logger.warning(f"Failed to cache client to {cache_file}: {e}")

    return client_id, client_secret


@pytest.fixture(scope="session")
async def read_only_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """
    Fixture for OAuth client with only read scopes.

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
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
        client_id, client_secret = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Test Client Read Only",
            allowed_scopes=DEFAULT_READ_SCOPES,
            token_type="JWT",  # JWT tokens for scope validation
        )

        return (
            client_id,
            client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )


@pytest.fixture(scope="session")
async def write_only_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """
    Fixture for OAuth client with only write scopes.

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
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
        client_id, client_secret = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Test Client Write Only",
            allowed_scopes=DEFAULT_WRITE_SCOPES,
            token_type="JWT",  # JWT tokens for scope validation
        )

        return (
            client_id,
            client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )


@pytest.fixture(scope="session")
async def full_access_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """
    Fixture for OAuth client with both read and write scopes.

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
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
        client_id, client_secret = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Test Client Full Access",
            allowed_scopes=DEFAULT_FULL_SCOPES,
            token_type="JWT",  # JWT tokens for scope validation
        )

        return (
            client_id,
            client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
        )


@pytest.fixture(scope="session")
async def no_custom_scopes_oauth_client_credentials(
    anyio_backend, oauth_callback_server
):
    """
    Fixture for OAuth client with NO custom scopes (only OIDC defaults).

    Tests the security behavior when a user grants only the default OIDC scopes
    (openid, profile, email) but declines custom application scopes (notes:read, notes:write, etc.).

    Returns:
        Tuple of (client_id, client_secret, callback_url, token_endpoint, authorization_endpoint)
    """
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
        client_id, client_secret = await _create_oauth_client_with_scopes(
            callback_url=callback_url,
            client_name="Test Client No Custom Scopes",
            allowed_scopes="openid profile email",  # No app-specific scopes (no app access)
            token_type="JWT",  # JWT tokens for scope validation
        )

        return (
            client_id,
            client_secret,
            callback_url,
            token_endpoint,
            authorization_endpoint,
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
    import secrets
    import time
    from urllib.parse import quote

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

    logger.info(f"Starting Playwright-based OAuth flow for {username}...")
    logger.info(f"Using shared OAuth client: {client_id[:16]}...")
    logger.info(f"Using real callback server at: {callback_url}")

    # Generate unique state parameter for this OAuth flow
    state = secrets.token_urlsafe(32)
    logger.debug(f"Generated state: {state[:16]}...")

    # Construct authorization URL with state parameter
    auth_url = (
        f"{authorization_endpoint}?"
        f"response_type=code&"
        f"client_id={client_id}&"
        f"redirect_uri={quote(callback_url, safe='')}&"
        f"state={state}&"
        f"scope=openid%20profile%20email%20notes:read%20notes:write%20calendar:read%20calendar:write%20contacts:read%20contacts:write%20cookbook:read%20cookbook:write%20deck:read%20deck:write%20tables:read%20tables:write%20files:read%20files:write%20sharing:read%20sharing:write"
    )

    # Async browser automation using pytest-playwright's browser fixture
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        # Navigate to authorization URL
        logger.debug(f"Navigating to: {auth_url}")
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)

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
            await page.wait_for_load_state("networkidle", timeout=60000)
            current_url = page.url
            logger.info(f"After login, current URL: {current_url}")

        # Handle consent screen if present
        try:
            await _handle_oauth_consent_screen(page, username)
        except Exception as e:
            logger.debug(f"No consent screen or already authorized: {e}")

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
                logger.error(f"Screenshot saved to {screenshot_path}")
                raise TimeoutError(
                    f"Timeout waiting for OAuth callback (state={state[:16]}...)"
                )
            await anyio.sleep(0.5)

        auth_code = auth_states[state]
        logger.info(f"Successfully received authorization code: {auth_code[:20]}...")

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
            logger.debug(f"No consent screen found for {username}")
            return False

        logger.info(f"Consent screen detected for {username}")

        # Get consent screen data attributes
        client_name = await consent_div.get_attribute("data-client-name")
        scopes_attr = await consent_div.get_attribute("data-scopes")
        logger.info(f"  Client: {client_name}")
        logger.info(f"  Requested scopes: {scopes_attr}")

        # Wait for Vue.js to render the Allow button (max 10 seconds)
        try:
            await page.wait_for_selector('button:has-text("Allow")', timeout=10000)
            logger.info("  Allow button rendered by Vue.js")
        except Exception as e:
            logger.warning(f"  Timeout waiting for Allow button: {e}")
            # Take a screenshot for debugging
            screenshot_path = f"/tmp/consent_no_allow_button_{username}.png"
            await page.screenshot(path=screenshot_path)
            logger.error(f"  Screenshot saved to {screenshot_path}")
            raise

        # Check all scope checkboxes
        scope_checkboxes = await page.query_selector_all('input[type="checkbox"]')
        if scope_checkboxes:
            logger.info(f"  Found {len(scope_checkboxes)} scope checkboxes")
            for i, checkbox in enumerate(scope_checkboxes):
                # Check if checkbox is not already checked
                is_checked = await checkbox.is_checked()
                is_disabled = await checkbox.is_disabled()
                if not is_checked and not is_disabled:
                    await checkbox.check()
                    logger.info(f"    ✓ Checked scope checkbox {i + 1}")
                elif is_checked:
                    logger.info(f"    ✓ Scope checkbox {i + 1} already checked")
                elif is_disabled:
                    logger.info(
                        f"    ⊗ Scope checkbox {i + 1} disabled (required scope)"
                    )

        # Click the Allow button to grant consent
        # Check button exists first
        allow_button_locator = page.locator('button:has-text("Allow")')

        if await allow_button_locator.count() > 0:
            logger.info(f"  Clicking Allow button to grant consent for {username}...")

            # Use JavaScript click to handle consent buttons that may be outside viewport
            # This is more reliable than Playwright's click which requires element visibility
            logger.info(
                "  Using JavaScript click for consent (handles viewport issues)..."
            )
            await page.evaluate(
                """
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.textContent.trim() === 'Allow') {
                        btn.click();
                        break;
                    }
                }
                """
            )

            await page.wait_for_load_state("networkidle", timeout=30000)
            logger.info(f"  Consent granted for {username}")
            return True
        else:
            logger.error(f"  Allow button not found for {username}")
            return False

    except Exception as e:
        logger.error(f"Error handling consent screen for {username}: {e}")
        raise


async def _get_oauth_token_with_scopes(
    browser,
    shared_oauth_client_credentials,
    oauth_callback_server,
    scopes: str,
) -> str:
    """
    Helper function to obtain OAuth token with specific scopes.

    Args:
        browser: Playwright browser instance
        shared_oauth_client_credentials: Tuple of OAuth client credentials
        oauth_callback_server: OAuth callback server fixture
        scopes: Space-separated list of scopes (e.g., "openid profile email notes:read")

    Returns:
        OAuth access token string with requested scopes
    """
    import secrets
    import time
    from urllib.parse import quote

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

    logger.info(f"Starting Playwright-based OAuth flow with scopes: {scopes}")
    logger.info(f"Using shared OAuth client: {client_id[:16]}...")
    logger.info(f"Using real callback server at: {callback_url}")

    # Generate unique state parameter for this OAuth flow
    state = secrets.token_urlsafe(32)
    logger.debug(f"Generated state: {state[:16]}...")

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

    # Async browser automation using pytest-playwright's browser fixture
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        # Navigate to authorization URL
        logger.debug(f"Navigating to: {auth_url}")
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)

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
            await page.wait_for_load_state("networkidle", timeout=60000)
            current_url = page.url
            logger.info(f"After login, current URL: {current_url}")

        # Handle consent screen if present
        try:
            await _handle_oauth_consent_screen(page, username)
        except Exception as e:
            logger.debug(f"No consent screen or already authorized: {e}")

        # Wait for callback server to receive the auth code
        logger.info(f"Waiting for auth code with state: {state[:16]}...")
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

        logger.info(f"Successfully obtained OAuth access token with scopes: {scopes}")
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

    logger.info("Creating test users for multi-user OAuth testing...")
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
                        f"Group creation returned {response.status_code}: {response.text}"
                    )
        except Exception as e:
            logger.warning(f"Error creating editors group (may already exist): {e}")

        # Create each test user
        for username, config in test_user_configs.items():
            try:
                await nc_client.users.create_user(
                    userid=username,
                    password=config["password"],
                    display_name=config["display_name"],
                    email=config["email"],
                )
                logger.info(f"Created test user: {username}")
                created_users.append(username)

                # Add user to groups if specified
                for group in config["groups"]:
                    try:
                        await nc_client.users.add_user_to_group(username, group)
                        logger.info(f"Added {username} to group {group}")
                    except Exception as e:
                        logger.warning(f"Error adding {username} to group {group}: {e}")

            except Exception as e:
                # User might already exist, that's okay
                logger.warning(
                    f"Could not create user {username} (may already exist): {e}"
                )
                created_users.append(username)  # Add to list anyway for cleanup

        logger.info(f"Test users setup complete: {created_users}")
        yield test_user_configs

    finally:
        # Cleanup: delete test users
        logger.info("Cleaning up test users...")
        for username in created_users:
            try:
                await nc_client.users.delete_user(username)
                logger.info(f"Deleted test user: {username}")
            except Exception as e:
                logger.warning(f"Error deleting test user {username}: {e}")


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
    import secrets
    import time
    from urllib.parse import quote

    nextcloud_host = os.getenv("NEXTCLOUD_HOST")

    if not nextcloud_host:
        pytest.skip("OAuth requires NEXTCLOUD_HOST")

    # Unpack shared client credentials
    client_id, client_secret, callback_url, token_endpoint, authorization_endpoint = (
        shared_oauth_client_credentials
    )

    logger.info(f"Getting OAuth token for user: {username}...")
    logger.info(f"Using shared OAuth client: {client_id[:16]}...")

    # Generate unique state parameter for this OAuth flow
    state = secrets.token_urlsafe(32)
    logger.debug(f"Generated state for {username}: {state[:16]}...")

    # Construct authorization URL with state parameter
    auth_url = (
        f"{authorization_endpoint}?"
        f"response_type=code&"
        f"client_id={client_id}&"
        f"redirect_uri={quote(callback_url, safe='')}&"
        f"state={state}&"
        f"scope=openid%20profile%20email%20notes:read%20notes:write%20calendar:read%20calendar:write%20contacts:read%20contacts:write%20cookbook:read%20cookbook:write%20deck:read%20deck:write%20tables:read%20tables:write%20files:read%20files:write%20sharing:read%20sharing:write"
    )

    logger.info(f"Performing browser OAuth flow for {username}...")
    logger.debug(f"Authorization URL: {auth_url}")

    # Browser automation
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
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

        # Handle consent screen if present
        try:
            await _handle_oauth_consent_screen(page, username)
        except Exception as e:
            logger.debug(f"No consent screen or already authorized for {username}: {e}")

        # Wait for callback server to receive the auth code
        # Browser will be redirected to localhost:8081 which will capture the code
        logger.info(
            f"Waiting for callback server to receive auth code for {username}..."
        )
        timeout_seconds = 30
        start_time = time.time()
        while state not in auth_states:
            if time.time() - start_time > timeout_seconds:
                # Take screenshot for debugging
                screenshot_path = f"/tmp/playwright_oauth_timeout_{username}.png"
                await page.screenshot(path=screenshot_path)
                logger.error(f"Screenshot saved to {screenshot_path}")
                raise TimeoutError(
                    f"Timeout waiting for OAuth callback for {username} (state={state[:16]}...)"
                )
            await anyio.sleep(0.5)

        auth_code = auth_states[state]
        logger.info(f"Got auth code for {username}: {auth_code[:20]}...")

    finally:
        await context.close()

    # Exchange code for token
    logger.info(f"Exchanging auth code for access token ({username})...")
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

        logger.info(f"Successfully obtained OAuth token for {username}")
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
    import time

    # Get auth_states dict from callback server
    auth_states, callback_url = oauth_callback_server

    start_time = time.time()
    logger.info("Fetching OAuth tokens for all users in parallel...")
    logger.info(f"Using callback server at {callback_url} with state-based correlation")

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
            logger.error(f"Failed to get OAuth token for {username}: {result}")
            raise result
        tokens[username] = result

    elapsed = time.time() - start_time
    logger.info(
        f"Successfully fetched {len(tokens)} OAuth tokens in parallel "
        f"in {elapsed:.1f}s (~{elapsed / len(tokens):.1f}s per user)"
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
    async for session in create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=alice_oauth_token,
        client_name="Alice MCP",
    ):
        yield session


@pytest.fixture(scope="session")
async def bob_mcp_client(
    anyio_backend, bob_oauth_token: str
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated as bob (viewer role)."""
    async for session in create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=bob_oauth_token,
        client_name="Bob MCP",
    ):
        yield session


@pytest.fixture(scope="session")
async def charlie_mcp_client(
    anyio_backend,
    charlie_oauth_token: str,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated as charlie (editor role, in 'editors' group)."""
    async for session in create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=charlie_oauth_token,
        client_name="Charlie MCP",
    ):
        yield session


@pytest.fixture(scope="session")
async def diana_mcp_client(
    anyio_backend,
    diana_oauth_token: str,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP client authenticated as diana (no-access role)."""
    async for session in create_mcp_client_session(
        url="http://localhost:8001/mcp",
        token=diana_oauth_token,
        client_name="Diana MCP",
    ):
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
    import uuid

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
        logger.debug(f"Cleaned up test user: {userid}")
    except Exception as e:
        logger.warning(f"Failed to cleanup test user {userid}: {e}")


@pytest.fixture
async def test_group(nc_client: NextcloudClient):
    """
    Fixture that creates a test group and cleans it up after the test.

    Returns the group ID.
    """
    import uuid

    # Generate unique group ID to avoid conflicts
    groupid = f"testgroup_{uuid.uuid4().hex[:8]}"

    # Cleanup before (in case of previous failed run)
    try:
        await nc_client.groups.delete_group(groupid)
    except Exception:
        pass

    # Create the group
    await nc_client.groups.create_group(groupid)
    logger.debug(f"Created test group: {groupid}")

    yield groupid

    # Cleanup after test
    try:
        await nc_client.groups.delete_group(groupid)
        logger.debug(f"Cleaned up test group: {groupid}")
    except Exception as e:
        logger.warning(f"Failed to cleanup test group {groupid}: {e}")


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
    logger.debug(f"Added user {user_config['userid']} to group {groupid}")

    yield (user_config, groupid)
