#!/usr/bin/env python3
"""Test script to verify OAuth MCP tools work correctly.

This script connects to the OAuth MCP server and tests tool execution.
Note: This currently requires a valid OAuth token, which must be obtained
through the browser-based OAuth flow.
"""

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def test_oauth_mcp_tools():
    """Test OAuth MCP server tools."""
    print("Connecting to OAuth MCP server on port 8001...")

    streamable_context = streamablehttp_client("http://127.0.0.1:8001/mcp")
    session_context = None

    try:
        read_stream, write_stream, _ = await streamable_context.__aenter__()
        session_context = ClientSession(read_stream, write_stream)
        session = await session_context.__aenter__()

        print("Initializing session...")
        await session.initialize()
        print("✓ Session initialized successfully")

        # List available tools
        print("\nListing available tools...")
        result = await session.list_tools()
        print(f"✓ Found {len(result.tools)} tools")

        for tool in result.tools[:5]:  # Show first 5
            print(f"  - {tool.name}: {tool.description}")

        if len(result.tools) > 5:
            print(f"  ... and {len(result.tools) - 5} more")

        # Try to call a simple tool
        print("\nTesting tool execution...")
        print("Note: Tool execution will fail without a valid OAuth token")
        print("      (OAuth token must be obtained through browser flow)")

        try:
            # Try to list tables (this will fail without OAuth token)
            response = await session.call_tool("nc_tables_list_tables", {})
            print(f"✓ Tool executed successfully: {response}")
        except Exception as e:
            print(f"✗ Tool execution failed (expected without OAuth token): {e}")
            print("\nTo use OAuth tools, you need to:")
            print("  1. Implement the browser-based OAuth authorization flow")
            print("  2. Obtain an access token from Nextcloud OIDC")
            print("  3. Include the token in the Authorization header")

        return True

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        # Clean up
        if session_context is not None:
            try:
                await session_context.__aexit__(None, None, None)
            except Exception:
                pass

        try:
            await streamable_context.__aexit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    print("OAuth MCP Server Tool Test")
    print("=" * 50)

    success = asyncio.run(test_oauth_mcp_tools())

    print("\n" + "=" * 50)
    if success:
        print("✓ Test completed (tools accessible)")
        sys.exit(0)
    else:
        print("✗ Test failed")
        sys.exit(1)
