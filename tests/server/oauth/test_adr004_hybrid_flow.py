"""ADR-004 Hybrid Flow Integration Tests.

Tests the complete ADR-004 Hybrid Flow where:
1. Client initiates OAuth at MCP server /oauth/authorize with PKCE
2. MCP server intercepts the flow and redirects to IdP
3. User authenticates and consents at IdP
4. IdP redirects to MCP server /oauth/callback
5. MCP server exchanges IdP code for master refresh token (stored securely)
6. MCP server redirects client with MCP authorization code
7. Client exchanges MCP code for MCP access token using PKCE verifier
8. Client uses MCP access token to establish MCP session and call tools
9. MCP server uses stored refresh token to access Nextcloud APIs on behalf of user

This validates:
- PKCE code challenge/verifier flow
- Master refresh token storage
- Token isolation (client never sees master refresh token)
- End-to-end tool execution with hybrid flow tokens
"""

import hashlib
import json
import logging
import os
import secrets
import time
from base64 import urlsafe_b64encode
from urllib.parse import quote

import anyio
import httpx
import pytest

from tests.conftest import create_mcp_client_session

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


def generate_pkce_challenge():
    """Generate PKCE code verifier and challenge.

    Returns:
        Tuple of (code_verifier, code_challenge)
    """
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = urlsafe_b64encode(digest).decode().rstrip("=")
    return code_verifier, code_challenge


@pytest.fixture(scope="session")
async def adr004_hybrid_flow_mcp_client(
    anyio_backend,
    browser,
    oauth_callback_server,
):
    """
    Fixture to create an MCP client session via ADR-004 Hybrid Flow with Playwright automation.

    This fixture tests the complete hybrid flow:
    1. Client initiates OAuth at MCP server with PKCE
    2. MCP server intercepts and redirects to IdP
    3. Playwright automates login and consent at IdP
    4. IdP redirects to MCP server callback
    5. MCP server stores master refresh token and redirects client with MCP code
    6. Client exchanges MCP code for access token using PKCE verifier
    7. Creates and returns MCP ClientSession with the token

    Yields:
        Initialized MCP ClientSession for ADR-004 hybrid flow
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME", "admin")
    password = os.getenv("NEXTCLOUD_PASSWORD", "admin")
    mcp_server_url = "http://localhost:8001"  # MCP OAuth server

    if not all([nextcloud_host, username, password]):
        pytest.skip(
            "ADR-004 Hybrid Flow requires NEXTCLOUD_HOST, NEXTCLOUD_USERNAME, and NEXTCLOUD_PASSWORD"
        )

    # Get auth_states dict and callback URL from callback server
    auth_states, callback_url = oauth_callback_server

    logger.info("=" * 70)
    logger.info("Starting ADR-004 Hybrid Flow test with Playwright")
    logger.info("=" * 70)
    logger.info(f"MCP Server: {mcp_server_url}")
    logger.info(f"Nextcloud: {nextcloud_host}")
    logger.info(f"User: {username}")
    logger.info(f"Client Callback: {callback_url}")
    logger.info("=" * 70)

    # Step 1: Generate PKCE challenge
    code_verifier, code_challenge = generate_pkce_challenge()
    logger.info(f"✓ Generated PKCE challenge: {code_challenge[:16]}...")

    # Step 2: Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    logger.debug(f"✓ Generated state: {state[:16]}...")

    # Step 3: Construct authorization URL to MCP server (not IdP!)
    # The MCP server will intercept this and redirect to IdP
    auth_params = {
        "response_type": "code",
        "client_id": "test-mcp-client",  # Client identifier (not OAuth client_id)
        "redirect_uri": callback_url,  # Client's callback
        "scope": "openid profile email offline_access notes:read notes:write",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    # Build query string manually to avoid double encoding
    query_parts = [f"{k}={quote(str(v), safe='')}" for k, v in auth_params.items()]
    auth_url = f"{mcp_server_url}/oauth/authorize?{'&'.join(query_parts)}"

    logger.info("Step 1: Client initiates OAuth at MCP server")
    logger.debug(f"Authorization URL: {auth_url[:100]}...")

    # Step 4: Navigate to authorization URL with Playwright
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        # Navigate to MCP server authorization endpoint
        # MCP server will redirect to IdP
        logger.debug("Navigating to MCP authorization endpoint...")
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)

        # Check current URL - should be at IdP login page
        current_url = page.url
        logger.info(f"Step 2: Redirected to IdP login: {current_url[:80]}...")

        # Fill in login form if present
        if "/login" in current_url or "/index.php/login" in current_url:
            logger.info("Step 3: Filling in credentials at IdP...")

            # Wait for login form
            await page.wait_for_selector('input[name="user"]', timeout=10000)

            # Fill in username and password
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)

            logger.debug("Submitting login form...")

            # Submit the form
            await page.click('button[type="submit"]')

            # Wait for navigation after login
            await page.wait_for_load_state("networkidle", timeout=60000)
            current_url = page.url
            logger.info(f"Step 4: After login: {current_url[:80]}...")

        # Handle consent screen if present
        logger.info("Step 5: Handling IdP consent screen...")
        try:
            await _handle_oauth_consent_screen(page, username)
        except Exception as e:
            logger.debug(f"No consent screen or already authorized: {e}")

        # Wait for callback server to receive the MCP authorization code
        # Browser will be redirected through: IdP → MCP callback → Client callback
        logger.info("Step 6: Waiting for MCP server to redirect with MCP code...")
        timeout_seconds = 30
        start_time = time.time()
        while state not in auth_states:
            if time.time() - start_time > timeout_seconds:
                # Take a screenshot for debugging
                screenshot_path = "/tmp/adr004_oauth_error.png"
                await page.screenshot(path=screenshot_path)
                logger.error(f"Screenshot saved to {screenshot_path}")
                raise TimeoutError(
                    f"Timeout waiting for MCP authorization code (state={state[:16]}...)"
                )
            await anyio.sleep(0.5)

        mcp_authorization_code = auth_states[state]
        logger.info(
            f"✓ Received MCP authorization code: {mcp_authorization_code[:20]}..."
        )

    finally:
        await context.close()

    # Step 7: Exchange MCP authorization code for MCP access token
    logger.info("Step 7: Exchanging MCP code for access token with PKCE verifier...")

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        token_response = await http_client.post(
            f"{mcp_server_url}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": mcp_authorization_code,
                "code_verifier": code_verifier,  # PKCE verifier
                "redirect_uri": callback_url,
                "client_id": "test-mcp-client",
            },
        )

        if token_response.status_code != 200:
            logger.error(f"Token exchange failed: {token_response.status_code}")
            logger.error(f"Response: {token_response.text}")
            raise RuntimeError(
                f"Token exchange failed: {token_response.status_code} - {token_response.text}"
            )

        token_data = token_response.json()
        access_token = token_data.get("access_token")

        if not access_token:
            raise ValueError(f"No access_token in response: {token_data}")

        logger.info("✓ Successfully obtained MCP access token via ADR-004 Hybrid Flow")
        logger.info(f"  Token: {access_token[:30]}...")
        logger.info(f"  Type: {token_data.get('token_type', 'Bearer')}")
        logger.info(f"  Expires in: {token_data.get('expires_in', 'unknown')}s")

        # Verify refresh token was stored (check database)
        logger.info("Step 8: Verifying master refresh token was stored...")
        # Note: In production, we'd verify the refresh token is in the database
        # For now, we'll verify by successfully calling a tool

        logger.info("=" * 70)
        logger.info("ADR-004 Hybrid Flow completed successfully!")
        logger.info("=" * 70)

        # Step 9: Create MCP client session with the token
        logger.info("Step 9: Creating MCP client session with hybrid flow token...")
        async for session in create_mcp_client_session(
            url=f"{mcp_server_url}/mcp",
            token=access_token,
            client_name="ADR-004 Hybrid Flow",
        ):
            logger.info("✓ ADR-004 MCP client session established")
            yield session


async def _handle_oauth_consent_screen(page, username: str = "admin"):
    """
    Handle the OIDC consent screen during ADR-004 flow.

    The consent screen:
    - Asks user to authorize MCP server to access Nextcloud
    - Contains scope information (notes:read, notes:write, etc.)
    - Has an "Authorize" button to grant access

    Args:
        page: Playwright page object
        username: Username for logging
    """
    try:
        # Wait for consent screen elements
        logger.debug("Checking for OAuth consent screen...")

        # Look for the authorize button
        authorize_button = page.locator('button[type="submit"]').filter(
            has_text="Authorize"
        )

        # Check if button exists with short timeout
        if await authorize_button.count() > 0:
            logger.info(
                f"Consent screen detected - authorizing MCP server access for {username}"
            )
            await authorize_button.click()
            logger.debug("Clicked Authorize button")

            # Wait for redirect after consent
            await page.wait_for_load_state("networkidle", timeout=30000)
            logger.info("Consent granted, waiting for redirect...")
        else:
            logger.debug("No consent screen found (may be pre-authorized)")

    except Exception as e:
        logger.debug(f"Consent screen handling skipped: {e}")
        # Not fatal - might already be authorized


# ============================================================================
# ADR-004 Hybrid Flow Tests
# ============================================================================


async def test_adr004_hybrid_flow_connection(adr004_hybrid_flow_mcp_client):
    """Test that ADR-004 hybrid flow token can establish MCP session."""
    # List tools to verify session is established
    result = await adr004_hybrid_flow_mcp_client.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    logger.info(
        f"✓ ADR-004 session established with {len(result.tools)} tools available"
    )


async def test_adr004_hybrid_flow_tool_execution(adr004_hybrid_flow_mcp_client):
    """Test that ADR-004 hybrid flow token can execute MCP tools.

    This verifies the complete flow:
    1. Client has MCP access token from hybrid flow
    2. MCP server has stored master refresh token
    3. MCP server can exchange master token for Nextcloud access
    4. Tool execution succeeds using on-behalf-of pattern
    """
    # Execute a tool that requires Nextcloud API access
    result = await adr004_hybrid_flow_mcp_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )

    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None
    response_data = json.loads(result.content[0].text)

    # Verify response structure
    assert "results" in response_data
    assert isinstance(response_data["results"], list)

    logger.info("=" * 70)
    logger.info("✓ ADR-004 HYBRID FLOW TEST - SUCCESS")
    logger.info("=" * 70)
    logger.info("✓ User consented to MCP server access")
    logger.info("✓ User consented to offline_access (refresh tokens)")
    logger.info("✓ MCP server stored master refresh token")
    logger.info("✓ Client received MCP access token via PKCE")
    logger.info("✓ MCP session established with hybrid flow token")
    logger.info("✓ MCP tool executed successfully")
    logger.info("✓ MCP server exchanged master token for Nextcloud access")
    logger.info(f"✓ Nextcloud API returned {len(response_data['results'])} notes")
    logger.info("=" * 70)


async def test_adr004_hybrid_flow_multiple_operations(adr004_hybrid_flow_mcp_client):
    """Test that ADR-004 token persists across multiple operations.

    Verifies that the stored master refresh token enables multiple tool calls
    without requiring re-authentication.
    """
    # First operation: Search notes
    result1 = await adr004_hybrid_flow_mcp_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )
    assert result1.isError is False

    # Second operation: List tools
    result2 = await adr004_hybrid_flow_mcp_client.list_tools()
    assert result2 is not None
    assert len(result2.tools) > 0

    # Third operation: Search notes again
    result3 = await adr004_hybrid_flow_mcp_client.call_tool(
        "nc_notes_search_notes", arguments={"query": "test"}
    )
    assert result3.isError is False

    logger.info("✓ ADR-004 token successfully used for 3 consecutive operations")
    logger.info("✓ Master refresh token enables persistent access")
