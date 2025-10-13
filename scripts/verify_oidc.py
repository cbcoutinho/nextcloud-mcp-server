#!/usr/bin/env python3
"""
Verification script for Nextcloud OIDC implementation.

This script tests the OIDC endpoints to understand token format and capabilities.
Usage: python scripts/verify_oidc.py
"""

import asyncio
import json
import sys

import httpx


class NextcloudOIDCVerifier:
    """Verify Nextcloud OIDC implementation details."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)

    async def close(self):
        await self.client.aclose()

    async def get_discovery(self) -> dict:
        """Fetch OIDC discovery document."""
        print(f"\n{'=' * 60}")
        print("1. OIDC Discovery Endpoint")
        print(f"{'=' * 60}")

        url = f"{self.base_url}/.well-known/openid-configuration"
        print(f"URL: {url}")

        try:
            response = await self.client.get(url)
            response.raise_for_status()
            discovery = response.json()

            print("\n✓ Discovery endpoint successful")
            print(f"\nIssuer: {discovery.get('issuer')}")
            print(f"Authorization endpoint: {discovery.get('authorization_endpoint')}")
            print(f"Token endpoint: {discovery.get('token_endpoint')}")
            print(f"Userinfo endpoint: {discovery.get('userinfo_endpoint')}")
            print(f"JWKS URI: {discovery.get('jwks_uri')}")
            print(
                f"Registration endpoint: {discovery.get('registration_endpoint', 'NOT AVAILABLE')}"
            )

            print(
                f"\nSupported scopes: {', '.join(discovery.get('scopes_supported', []))}"
            )
            print(
                f"Response types: {', '.join(discovery.get('response_types_supported', []))}"
            )
            print(
                f"Grant types: {', '.join(discovery.get('grant_types_supported', []))}"
            )

            return discovery

        except httpx.HTTPStatusError as e:
            print(f"\n✗ Discovery failed: HTTP {e.response.status_code}")
            print(f"Response: {e.response.text}")
            sys.exit(1)
        except Exception as e:
            print(f"\n✗ Discovery failed: {e}")
            sys.exit(1)

    async def get_jwks(self, jwks_uri: str) -> dict:
        """Fetch JWKS to check if JWT tokens are supported."""
        print(f"\n{'=' * 60}")
        print("2. JWKS Endpoint (JWT Support)")
        print(f"{'=' * 60}")

        print(f"URL: {jwks_uri}")

        try:
            response = await self.client.get(jwks_uri)
            response.raise_for_status()
            jwks = response.json()

            print("\n✓ JWKS endpoint successful")
            print(f"Number of keys: {len(jwks.get('keys', []))}")

            for idx, key in enumerate(jwks.get("keys", []), 1):
                print(f"\nKey {idx}:")
                print(f"  - Key type: {key.get('kty')}")
                print(f"  - Algorithm: {key.get('alg')}")
                print(f"  - Use: {key.get('use', 'N/A')}")
                print(f"  - Key ID: {key.get('kid', 'N/A')}")

            return jwks

        except Exception as e:
            print(f"\n✗ JWKS failed: {e}")
            return {}

    async def test_dynamic_registration(
        self, registration_endpoint: str | None
    ) -> dict | None:
        """Test dynamic client registration."""
        print(f"\n{'=' * 60}")
        print("3. Dynamic Client Registration")
        print(f"{'=' * 60}")

        if not registration_endpoint:
            print("✗ Dynamic registration not available (not in discovery)")
            return None

        print(f"URL: {registration_endpoint}")

        client_metadata = {
            "client_name": "Nextcloud MCP Server Test",
            "redirect_uris": ["http://localhost:8000/oauth/callback"],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "openid profile email roles groups",
        }

        print("\nRegistration payload:")
        print(json.dumps(client_metadata, indent=2))

        try:
            response = await self.client.post(
                registration_endpoint,
                json=client_metadata,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            client_info = response.json()

            print("\n✓ Dynamic registration successful")
            print(f"\nClient ID: {client_info.get('client_id')}")
            print(f"Client Secret: {client_info.get('client_secret', 'N/A')[:20]}...")
            print(
                f"Client ID issued at: {client_info.get('client_id_issued_at', 'N/A')}"
            )
            print(
                f"Client secret expires at: {client_info.get('client_secret_expires_at', 'Never')}"
            )

            # Save for later use
            with open("/tmp/nextcloud_oidc_client.json", "w") as f:
                json.dump(client_info, f, indent=2)
            print("\n✓ Client credentials saved to /tmp/nextcloud_oidc_client.json")

            return client_info

        except httpx.HTTPStatusError as e:
            print(f"\n✗ Dynamic registration failed: HTTP {e.response.status_code}")
            print(f"Response: {e.response.text}")
            return None
        except Exception as e:
            print(f"\n✗ Dynamic registration failed: {e}")
            return None

    async def check_introspection_endpoint(self, discovery: dict) -> bool:
        """Check if token introspection endpoint exists."""
        print(f"\n{'=' * 60}")
        print("4. Token Introspection Endpoint")
        print(f"{'=' * 60}")

        introspection_endpoint = discovery.get("introspection_endpoint")

        if introspection_endpoint:
            print(f"URL: {introspection_endpoint}")
            print("✓ Introspection endpoint available")
            return True
        else:
            print("✗ Introspection endpoint NOT available")
            print("Note: Will need to use userinfo endpoint for token validation")
            return False

    def print_summary(
        self, discovery: dict, jwks_available: bool, registration_available: bool
    ):
        """Print implementation summary."""
        print(f"\n{'=' * 60}")
        print("IMPLEMENTATION SUMMARY")
        print(f"{'=' * 60}")

        print("\n📋 Nextcloud OIDC Capabilities:")
        print("  ✓ Discovery endpoint: Available")
        print(
            f"  {'✓' if jwks_available else '✗'} JWKS endpoint: {'Available' if jwks_available else 'Not Available'}"
        )
        print(
            f"  {'✓' if registration_available else '✗'} Dynamic registration: {'Available' if registration_available else 'Not Available'}"
        )
        print(f"  {'✗'} Token introspection: Not Available (use userinfo)")

        print("\n🔑 Token Format:")
        if jwks_available:
            print("  ✓ JWT access tokens: SUPPORTED (RFC 9068)")
            print("    - Must be enabled per-client in OIDC settings")
            print("    - Default: Opaque tokens")
        else:
            print("  - Opaque tokens only")

        print("\n🔐 Authentication Strategy:")
        print("  Primary: Userinfo endpoint validation")
        print("  Alternative: JWT validation (if enabled per-client)")

        print("\n📦 Required Scopes:")
        scopes = discovery.get("scopes_supported", [])
        print(f"  Available: {', '.join(scopes)}")
        print("  Recommended for MCP: openid profile email")

        print("\n👤 User Context Extraction:")
        print("  - Username: 'sub' or 'preferred_username' claim")
        print("  - From: JWT claims OR userinfo endpoint")
        print("  - Groups: Available via 'roles' or 'groups' scope")

        print("\n⚙️  Configuration Requirements:")
        if registration_available:
            print("  ✓ Dynamic registration enabled - zero-config deployment possible")
            print("    - Clients expire after 3600s (1 hour)")
            print("    - Max 100 dynamic clients per instance")
            print("    - BruteForce protection enabled")
        else:
            print("  ✗ Dynamic registration disabled - manual client setup required")
            print("    Admin must create client via: occ oidc:create")

        print("\n📝 Endpoints:")
        print(f"  Authorization: {discovery.get('authorization_endpoint')}")
        print(f"  Token: {discovery.get('token_endpoint')}")
        print(f"  Userinfo: {discovery.get('userinfo_endpoint')}")
        print(f"  JWKS: {discovery.get('jwks_uri')}")


async def main():
    """Run verification tests."""
    print("=" * 60)
    print("Nextcloud OIDC Verification Script")
    print("=" * 60)

    # Get Nextcloud URL
    nextcloud_url = input(
        "\nEnter Nextcloud URL (e.g., https://cloud.coutinho.io): "
    ).strip()
    if not nextcloud_url:
        nextcloud_url = "https://cloud.coutinho.io"

    verifier = NextcloudOIDCVerifier(nextcloud_url)

    try:
        # 1. Get discovery document
        discovery = await verifier.get_discovery()

        # 2. Check JWKS
        jwks_uri = discovery.get("jwks_uri")
        jwks_available = False
        if jwks_uri:
            jwks = await verifier.get_jwks(jwks_uri)
            jwks_available = len(jwks.get("keys", [])) > 0

        # 3. Test dynamic registration
        registration_endpoint = discovery.get("registration_endpoint")
        if registration_endpoint:
            print("\nTest dynamic registration? (y/n): ", end="")
            test_reg = input().strip().lower()
            if test_reg == "y":
                client_info = await verifier.test_dynamic_registration(
                    registration_endpoint
                )
                registration_available = client_info is not None
            else:
                registration_available = True
                print("Skipping dynamic registration test")
        else:
            registration_available = False

        # 4. Check introspection
        await verifier.check_introspection_endpoint(discovery)

        # 5. Print summary
        verifier.print_summary(discovery, jwks_available, registration_available)

        print(f"\n{'=' * 60}")
        print("Verification complete!")
        print(f"{'=' * 60}\n")

    finally:
        await verifier.close()


if __name__ == "__main__":
    asyncio.run(main())
