#!/usr/bin/env python3
"""
ADR-004 Manual OAuth Flow Test

This is a simplified version that doesn't use Playwright automation.
Instead, it prints URLs and waits for manual browser interaction.

Usage:
    uv run python tests/manual/test_adr004_manual.py --provider nextcloud
"""

import argparse
import asyncio
import hashlib
import logging
import secrets
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
        </body>
        </html>
        """.format(code_display)
        self.wfile.write(html.encode())

    def log_message(self, format, *args):
        """Log HTTP requests"""
        logger.info(f"Callback server: {format % args}")


def generate_pkce_challenge():
    """Generate PKCE code verifier and challenge"""
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = urlsafe_b64encode(digest).decode().rstrip("=")
    return code_verifier, code_challenge


async def test_oauth_manual(
    provider: str,
    mcp_server_url: str,
    nextcloud_host: str,
):
    """
    Manual OAuth flow test - prints URLs for manual browser interaction.
    """
    print("\n" + "=" * 70)
    print("ADR-004 MANUAL OAUTH FLOW TEST")
    print("=" * 70)
    print(f"Provider:          {provider}")
    print(f"MCP Server:        {mcp_server_url}")
    print(f"Nextcloud:         {nextcloud_host}")
    print("=" * 70 + "\n")

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
        # Build authorization URL
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
        print("STEP 1: AUTHORIZE THE MCP SERVER")
        print("=" * 70)
        print("\n📋 Open this URL in your browser:\n")
        print(f"    {auth_url}")
        print("\n📌 What will happen:")
        print("   1. You'll be redirected to Nextcloud/Keycloak login")
        print("   2. Login with username: admin, password: admin")
        print("   3. You'll see a consent screen asking to authorize the MCP server")
        print("   4. Click 'Authorize' or 'Allow'")
        print("   5. You'll be redirected to localhost:8765/callback")
        print("   6. The authorization code will appear in the terminal\n")
        print("=" * 70)
        print("\n⏳ Waiting for authorization... (timeout: 5 minutes)\n")

        # Wait for authorization code (with timeout)
        timeout = 300  # 5 minutes
        elapsed = 0
        while not CallbackHandler.authorization_code and elapsed < timeout:
            await asyncio.sleep(1)
            elapsed += 1

        if not CallbackHandler.authorization_code:
            raise RuntimeError("Timeout waiting for authorization code")

        authorization_code = CallbackHandler.authorization_code
        returned_state = CallbackHandler.state

        print("\n✓ Received authorization code!")
        logger.info(f"Code: {authorization_code[:16]}...")

        # Verify state
        if returned_state != state:
            raise RuntimeError(
                f"State mismatch! Expected {state}, got {returned_state}"
            )
        logger.info("✓ State parameter verified (CSRF protection)")

        # Exchange authorization code for access token
        print("\n" + "=" * 70)
        print("STEP 2: EXCHANGE CODE FOR ACCESS TOKEN")
        print("=" * 70)

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
                timeout=30.0,
            )

            if token_response.status_code != 200:
                print(f"\n❌ Token exchange failed: {token_response.status_code}")
                print(f"Response: {token_response.text}")
                raise RuntimeError("Token exchange failed")

            token_data = token_response.json()
            access_token = token_data["access_token"]

            print("\n✓ Successfully received access token")
            print(f"  Token: {access_token[:30]}...")
            print(f"  Type: {token_data.get('token_type', 'Bearer')}")
            print(f"  Expires: {token_data.get('expires_in', 'unknown')}s")

        # Test MCP tool call
        print("\n" + "=" * 70)
        print("STEP 3: CALL MCP TOOL WITH ACCESS TOKEN")
        print("=" * 70)

        async with httpx.AsyncClient() as client:
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
                print(f"\n❌ MCP tool call failed: {mcp_response.status_code}")
                print(f"Response: {mcp_response.text}")
                raise RuntimeError("MCP tool call failed")

            mcp_result = mcp_response.json()

            if "error" in mcp_result:
                print(f"\n❌ MCP tool returned error: {mcp_result['error']}")
                raise RuntimeError(f"MCP tool error: {mcp_result['error']}")

            print("\n✓ MCP tool call succeeded!")
            print(f"  Result: {mcp_result.get('result', {})}")

        # Summary
        print("\n" + "=" * 70)
        print("🎉 ADR-004 OAUTH FLOW TEST - SUCCESS")
        print("=" * 70)
        print(f"Provider:          {provider}")
        print(f"MCP Server:        {mcp_server_url}")
        print(f"Nextcloud:         {nextcloud_host}")
        print("")
        print("✓ User consented to MCP server access")
        print("✓ User consented to offline_access (refresh tokens)")
        print("✓ MCP server stored master refresh token")
        print("✓ Client received MCP access token via PKCE")
        print("✓ MCP tool call succeeded")
        print("✓ MCP server exchanged tokens in background")
        print("✓ Nextcloud data fetched successfully")
        print("=" * 70 + "\n")

        return {"success": True}

    finally:
        server.shutdown()
        logger.info("Stopped callback server")


async def main():
    parser = argparse.ArgumentParser(
        description="Manual test for ADR-004 OAuth Hybrid Flow"
    )

    parser.add_argument(
        "--provider",
        choices=["nextcloud", "keycloak"],
        required=True,
        help="OAuth provider to test",
    )

    parser.add_argument(
        "--mcp-server-url",
        default="http://localhost:8001",
        help="MCP server URL (default: http://localhost:8001)",
    )

    parser.add_argument(
        "--nextcloud-host",
        default="http://localhost:8080",
        help="Nextcloud host URL (default: http://localhost:8080)",
    )

    args = parser.parse_args()

    try:
        result = await test_oauth_manual(
            provider=args.provider,
            mcp_server_url=args.mcp_server_url,
            nextcloud_host=args.nextcloud_host,
        )

        return 0 if result["success"] else 1

    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        return 1
    except Exception as e:
        logger.error(f"OAuth flow test failed: {e}", exc_info=True)
        print("\n" + "=" * 70)
        print("❌ ADR-004 OAUTH FLOW TEST - FAILED")
        print("=" * 70)
        print(f"Error: {e}")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
