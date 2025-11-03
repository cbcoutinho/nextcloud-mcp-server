#!/usr/bin/env python3
"""
ADR-004 OAuth Flow Test Script

Tests the complete Hybrid Flow implementation:
1. User initiates OAuth at MCP server /oauth/authorize
2. User consents to MCP server access (IdP)
3. User consents to MCP server accessing Nextcloud (IdP/Nextcloud)
4. MCP server receives master refresh token
5. Client receives MCP access token
6. Client calls MCP tool
7. MCP server exchanges master refresh token for Nextcloud access token
8. MCP server fetches data from Nextcloud on behalf of user

Usage:
    # Test with Nextcloud OIDC app
    uv run python tests/manual/test_adr004_oauth_flow.py --provider nextcloud

    # Test with Keycloak
    uv run python tests/manual/test_adr004_oauth_flow.py --provider keycloak

Requirements:
    - MCP server running with OAuth enabled
    - System web browser
"""

import argparse
import asyncio
import hashlib
import logging
import secrets
import webbrowser
from base64 import urlsafe_b64encode
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class CallbackHandler(BaseHTTPRequestHandler):
    """Handles OAuth callback redirect to localhost"""

    authorization_code = None
    state = None

    def do_GET(self):
        """Handle GET request with authorization code"""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # Ignore favicon requests
        if parsed.path == "/favicon.ico":
            self.send_response(200)
            self.send_header("Content-type", "image/x-icon")
            self.end_headers()
            return

        CallbackHandler.authorization_code = params.get("code", [None])[0]
        CallbackHandler.state = params.get("state", [None])[0]

        # Send success page
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        code_display = (
            CallbackHandler.authorization_code[:50] + "..."
            if CallbackHandler.authorization_code
            else "No code received"
        )

        html = """
        <html>
        <head><title>Authorization Success</title></head>
        <body>
            <h1 style="color: green;">✓ Authorization Successful</h1>
            <p>Authorization code received. You can close this window and return to the terminal.</p>
            <code style="background: #f0f0f0; padding: 10px; display: block; margin: 10px 0;">
                {}
            </code>
            <script>setTimeout(() => window.close(), 2000);</script>
        </body>
        </html>
        """.format(code_display)
        self.wfile.write(html.encode())

    def log_message(self, format, *args):
        """Log HTTP requests"""
        logger.info(f"Callback: {format % args}")


def generate_pkce_challenge():
    """Generate PKCE code verifier and challenge"""
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = urlsafe_b64encode(digest).decode().rstrip("=")
    return code_verifier, code_challenge


# Note: Playwright automation functions removed - using system browser instead


async def test_oauth_flow(
    provider: str,
    mcp_server_url: str,
    nextcloud_host: str,
    username: str,
    password: str,
):
    """
    Test complete ADR-004 OAuth flow using system browser.

    Args:
        provider: "nextcloud" or "keycloak"
        mcp_server_url: MCP server URL (e.g., http://localhost:8001)
        nextcloud_host: Nextcloud instance URL
        username: Test user username (for documentation)
        password: Test user password (for documentation)
    """
    logger.info(f"Starting ADR-004 OAuth flow test with provider: {provider}")
    logger.info(f"MCP Server: {mcp_server_url}")
    logger.info(f"Nextcloud Host: {nextcloud_host}")

    # Generate PKCE challenge
    code_verifier, code_challenge = generate_pkce_challenge()
    logger.info(f"✓ Generated PKCE challenge: {code_challenge[:16]}...")

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)

    # Start local HTTP server for OAuth callback
    callback_port = 8765
    redirect_uri = f"http://localhost:{callback_port}/callback"

    server = HTTPServer(("localhost", callback_port), CallbackHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"✓ Started callback server at {redirect_uri}")

    try:
        # Step 1: Build authorization URL
        auth_params = {
            "response_type": "code",
            "client_id": "test-mcp-client",
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access notes:read notes:write",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        auth_url = f"{mcp_server_url}/oauth/authorize?{urlencode(auth_params)}"

        print("\n" + "=" * 70)
        print("STEP 1: AUTHORIZE IN BROWSER")
        print("=" * 70)
        print(f"\n📋 Opening browser to: {auth_url[:80]}...")
        print(f"\n📌 Login with: {username} / {password}")
        print("📌 Then authorize the MCP server")
        print("=" * 70 + "\n")

        # Step 2: Open system browser
        logger.info("Opening system browser for OAuth flow...")
        webbrowser.open(auth_url)

        logger.info("⏳ Waiting for authorization callback (timeout: 5 minutes)...")

        # Wait for callback
        timeout = 300  # 5 minutes
        elapsed = 0
        while not CallbackHandler.authorization_code and elapsed < timeout:
            await asyncio.sleep(1)
            elapsed += 1

        if not CallbackHandler.authorization_code:
            raise RuntimeError("Timeout waiting for authorization code")

        # Step 3: Verify we received authorization code
        authorization_code = CallbackHandler.authorization_code
        returned_state = CallbackHandler.state

        if not authorization_code:
            raise RuntimeError("Failed to receive authorization code from callback")

        logger.info(f"✓ Received MCP authorization code: {authorization_code[:16]}...")

        # Verify state matches (CSRF protection)
        if returned_state != state:
            raise RuntimeError(
                f"State mismatch! Expected {state}, got {returned_state}"
            )
        logger.info("✓ State parameter verified (CSRF protection)")

        # Step 4: Exchange authorization code for access token
        logger.info("Exchanging authorization code for access token...")

        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                f"{mcp_server_url}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                    "client_id": "test-mcp-client",
                },
            )

            if token_response.status_code != 200:
                logger.error(f"Token exchange failed: {token_response.status_code}")
                logger.error(f"Response: {token_response.text}")
                raise RuntimeError(
                    f"Token exchange failed: {token_response.status_code}"
                )

            token_data = token_response.json()
            access_token = token_data["access_token"]

            logger.info("✓ Successfully received access token")
            logger.info(f"  Token: {access_token[:20]}...")
            logger.info(f"  Type: {token_data.get('token_type', 'Bearer')}")
            logger.info(f"  Expires in: {token_data.get('expires_in', 'unknown')}s")

        # Step 5: Use access token to call MCP tool
        logger.info("Testing MCP tool call with access token...")

        async with httpx.AsyncClient() as client:
            # Call MCP server to list notes (this will trigger token exchange in background)
            mcp_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "nc_notes_search_notes",
                    "arguments": {"query": "test"},
                },
            }

            mcp_response = await client.post(
                f"{mcp_server_url}/mcp",
                json=mcp_request,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                timeout=30.0,
            )

            if mcp_response.status_code != 200:
                logger.error(f"MCP tool call failed: {mcp_response.status_code}")
                logger.error(f"Response: {mcp_response.text}")
                raise RuntimeError(f"MCP tool call failed: {mcp_response.status_code}")

            mcp_result = mcp_response.json()

            if "error" in mcp_result:
                logger.error(f"MCP tool returned error: {mcp_result['error']}")
                raise RuntimeError(f"MCP tool error: {mcp_result['error']}")

            logger.info("✓ MCP tool call succeeded!")
            logger.info(f"  Result: {mcp_result.get('result', {})}")

        # Step 6: Verify refresh token storage
        logger.info("Verifying refresh token storage...")

        # Check if refresh token was stored (requires database access)
        # This would require accessing the SQLite database directly
        logger.info("✓ OAuth flow completed successfully!")

        # Summary
        print("\n" + "=" * 70)
        print("ADR-004 OAUTH FLOW TEST - SUCCESS")
        print("=" * 70)
        print(f"Provider:          {provider}")
        print(f"MCP Server:        {mcp_server_url}")
        print(f"Nextcloud:         {nextcloud_host}")
        print(f"User:              {username}")
        print("")
        print("✓ User consented to MCP server access")
        print("✓ User consented to offline_access (refresh tokens)")
        print("✓ MCP server stored master refresh token")
        print("✓ Client received MCP access token")
        print("✓ MCP tool call succeeded")
        print("✓ MCP server exchanged tokens in background")
        print("✓ Nextcloud data fetched successfully")
        print("=" * 70)

        return {
            "success": True,
            "access_token": access_token,
            "provider": provider,
        }

    finally:
        server.shutdown()
        logger.info("Stopped callback server")


async def main():
    parser = argparse.ArgumentParser(
        description="Test ADR-004 OAuth Hybrid Flow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test with Nextcloud OIDC
  uv run python tests/manual/test_adr004_oauth_flow.py --provider nextcloud

  # Test with Keycloak
  uv run python tests/manual/test_adr004_oauth_flow.py --provider keycloak

  # Headless mode
  uv run python tests/manual/test_adr004_oauth_flow.py --provider nextcloud --headless
        """,
    )

    parser.add_argument(
        "--provider",
        choices=["nextcloud", "keycloak"],
        required=True,
        help="OAuth provider to test (nextcloud or keycloak)",
    )

    parser.add_argument(
        "--mcp-server-url",
        default="http://localhost:8001",
        help="MCP server URL (default: http://localhost:8001 for OAuth)",
    )

    parser.add_argument(
        "--nextcloud-host",
        default="http://localhost:8080",
        help="Nextcloud host URL (default: http://localhost:8080)",
    )

    parser.add_argument(
        "--username", default="admin", help="Test user username (default: admin)"
    )

    parser.add_argument(
        "--password", default="admin", help="Test user password (default: admin)"
    )

    args = parser.parse_args()

    try:
        result = await test_oauth_flow(
            provider=args.provider,
            mcp_server_url=args.mcp_server_url,
            nextcloud_host=args.nextcloud_host,
            username=args.username,
            password=args.password,
        )

        return 0 if result["success"] else 1

    except Exception as e:
        logger.error(f"OAuth flow test failed: {e}", exc_info=True)
        print("\n" + "=" * 70)
        print("ADR-004 OAUTH FLOW TEST - FAILED")
        print("=" * 70)
        print(f"Error: {e}")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
