"""
Test that DNS rebinding protection is properly disabled for containerized deployments.

This test verifies that the fix for MCP 1.23.x DNS rebinding protection works correctly.
Without the fix, requests with Host headers that don't match the default allowed list
(127.0.0.1:*, localhost:*, [::1]:*) would be rejected with a 421 Misdirected Request error.
"""

import httpx
import pytest


@pytest.mark.integration
async def test_accepts_various_host_headers():
    """Test that the MCP server accepts requests with various Host headers.

    This test simulates what happens in containerized deployments where the Host
    header might be a k8s service DNS name, a proxied hostname, or other values
    that don't match the default allowed list.

    Without the DNS rebinding protection fix, these requests would fail with:
    - 421 Misdirected Request (for Host header mismatch)
    - 403 Forbidden (for Origin header mismatch)
    """
    mcp_url = "http://localhost:8000/mcp"

    # Test various Host headers that would be rejected by DNS rebinding protection
    test_cases = [
        {
            "name": "Kubernetes service DNS",
            "headers": {
                "Host": "nextcloud-mcp-server.default.svc.cluster.local:8000",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        },
        {
            "name": "Custom domain",
            "headers": {
                "Host": "mcp.example.com:8000",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        },
        {
            "name": "Proxied hostname",
            "headers": {
                "Host": "proxy.internal:8000",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        },
        {
            "name": "Default localhost (should always work)",
            "headers": {
                "Host": "localhost:8000",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        },
    ]

    # Create a simple initialize request payload
    initialize_request = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
        "id": 1,
    }

    async with httpx.AsyncClient() as client:
        for test_case in test_cases:
            print(f"\n🧪 Testing: {test_case['name']}")
            print(f"   Host header: {test_case['headers']['Host']}")

            response = await client.post(
                mcp_url,
                json=initialize_request,
                headers=test_case["headers"],
                timeout=10.0,
            )

            # With DNS rebinding protection enabled (MCP 1.23 default), these would fail with:
            # - 421 Misdirected Request (Host header not in allowed list)
            # - 403 Forbidden (Origin header not in allowed list)
            #
            # With our fix (enable_dns_rebinding_protection=False), they should succeed
            assert response.status_code in [200, 202], (
                f"Request failed for {test_case['name']}: "
                f"status={response.status_code}, "
                f"headers={test_case['headers']}, "
                f"body={response.text[:200]}"
            )

            print(f"   ✅ Status: {response.status_code}")

            # For SSE responses (status 200), verify we got SSE format
            # For JSON responses (status 202), verify we got valid JSON
            if response.status_code == 200:
                # SSE response - should start with "event: message" or similar
                response_text = response.text
                assert "event:" in response_text or "data:" in response_text, (
                    f"Expected SSE format for {test_case['name']}, got: {response_text[:200]}"
                )
                print("   ✅ Received SSE stream response")
            elif response.status_code == 202:
                # JSON response for notifications
                response_json = response.json()
                assert "jsonrpc" in response_json or response_json is None, (
                    f"Invalid response for {test_case['name']}: {response_json}"
                )
                print("   ✅ Received JSON response")


@pytest.mark.integration
async def test_dns_rebinding_protection_is_disabled():
    """Verify that DNS rebinding protection is actually disabled in the configuration.

    This test makes a request that would DEFINITELY fail if DNS rebinding protection
    was enabled with default settings (only allowing 127.0.0.1:*, localhost:*, [::1]:*).
    """
    mcp_url = "http://localhost:8000/mcp"

    # Use a Host header that would NEVER be in the default allowed list
    malicious_host = "evil.attacker.com:8000"

    initialize_request = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
        "id": 1,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            mcp_url,
            json=initialize_request,
            headers={
                "Host": malicious_host,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=10.0,
        )

        # If DNS rebinding protection was enabled, this would return:
        # - 421 Misdirected Request (Host header validation failed)
        #
        # Since we disabled it, this should succeed (status 200 or 202)
        assert response.status_code in [200, 202], (
            f"DNS rebinding protection may still be enabled! "
            f"Request with Host='{malicious_host}' was rejected: "
            f"status={response.status_code}, body={response.text[:500]}"
        )

        # Verify we got a valid response (SSE or JSON)
        if response.status_code == 200:
            response_text = response.text
            assert "event:" in response_text or "data:" in response_text, (
                f"Expected SSE format, got: {response_text[:200]}"
            )

        print("✅ DNS rebinding protection is properly disabled")
        print(
            f"   Request with Host '{malicious_host}' succeeded: {response.status_code}"
        )
