"""
Keycloak OAuth 2.0 / OIDC Client

Handles OAuth flows with Keycloak as the identity provider, including:
- OIDC Discovery
- Authorization Code Flow with PKCE
- Token refresh using refresh tokens (ADR-002 Tier 1)
- Integration with RefreshTokenStorage
"""

import hashlib
import logging
import os
import secrets
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx

from nextcloud_mcp_server.auth.client_registration import generate_state, verify_state

logger = logging.getLogger(__name__)


class KeycloakOAuthClient:
    """OAuth 2.0 client for Keycloak integration"""

    def __init__(
        self,
        keycloak_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: Optional[list[str]] = None,
    ):
        """
        Initialize Keycloak OAuth client.

        Args:
            keycloak_url: Base URL of Keycloak (e.g., http://keycloak:8080)
            realm: Keycloak realm name
            client_id: OAuth client ID
            client_secret: OAuth client secret
            redirect_uri: OAuth redirect URI
            scopes: List of scopes to request (default: openid, profile, email, offline_access)
        """
        self.keycloak_url = keycloak_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes or ["openid", "profile", "email", "offline_access"]

        # Discovered endpoints (populated by discover())
        self.authorization_endpoint: Optional[str] = None
        self.token_endpoint: Optional[str] = None
        self.userinfo_endpoint: Optional[str] = None
        self.jwks_uri: Optional[str] = None
        self.end_session_endpoint: Optional[str] = None

        self._http_client: Optional[httpx.AsyncClient] = None

    @classmethod
    def from_env(cls) -> "KeycloakOAuthClient":
        """
        Create client from environment variables.

        Environment variables:
            KEYCLOAK_URL: Keycloak base URL
            KEYCLOAK_REALM: Realm name
            KEYCLOAK_CLIENT_ID: Client ID
            KEYCLOAK_CLIENT_SECRET: Client secret
            NEXTCLOUD_MCP_SERVER_URL: MCP server URL (for redirect URI)

        Returns:
            KeycloakOAuthClient instance

        Raises:
            ValueError: If required environment variables are missing
        """
        keycloak_url = os.getenv("KEYCLOAK_URL")
        realm = os.getenv("KEYCLOAK_REALM")
        client_id = os.getenv("KEYCLOAK_CLIENT_ID")
        client_secret = os.getenv("KEYCLOAK_CLIENT_SECRET")
        server_url = os.getenv("NEXTCLOUD_MCP_SERVER_URL", "http://localhost:8000")

        if not all([keycloak_url, realm, client_id, client_secret]):
            raise ValueError(
                "Missing required environment variables: "
                "KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_CLIENT_ID, KEYCLOAK_CLIENT_SECRET"
            )

        # Parse server URL to construct redirect URI
        parsed_url = urlparse(server_url)
        redirect_uri = f"{parsed_url.scheme}://{parsed_url.netloc}/oauth/callback"

        return cls(
            keycloak_url=keycloak_url,
            realm=realm,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client"""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def close(self) -> None:
        """Close HTTP client"""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def discover(self) -> None:
        """
        Perform OIDC discovery to get endpoint URLs.

        Raises:
            httpx.HTTPError: If discovery fails
        """
        discovery_url = (
            f"{self.keycloak_url}/realms/{self.realm}/.well-known/openid-configuration"
        )

        logger.info(f"Discovering Keycloak endpoints at {discovery_url}")

        client = await self._get_http_client()
        response = await client.get(discovery_url)
        response.raise_for_status()

        discovery_data = response.json()

        self.authorization_endpoint = discovery_data["authorization_endpoint"]
        self.token_endpoint = discovery_data["token_endpoint"]
        self.userinfo_endpoint = discovery_data["userinfo_endpoint"]
        self.jwks_uri = discovery_data.get("jwks_uri")
        self.end_session_endpoint = discovery_data.get("end_session_endpoint")

        logger.info(
            f"✓ Discovered Keycloak endpoints:\n"
            f"  Authorization: {self.authorization_endpoint}\n"
            f"  Token: {self.token_endpoint}\n"
            f"  Userinfo: {self.userinfo_endpoint}\n"
            f"  JWKS: {self.jwks_uri}"
        )

    def generate_pkce_challenge(self) -> tuple[str, str]:
        """
        Generate PKCE code verifier and challenge.

        Returns:
            Tuple of (code_verifier, code_challenge)
        """
        import base64

        # Generate code verifier (43-128 characters)
        code_verifier = secrets.token_urlsafe(32)

        # Generate code challenge using S256 method (base64url-encoded SHA256)
        digest = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")

        return code_verifier, code_challenge

    async def get_authorization_url(
        self,
        state: str,
        code_challenge: str,
        extra_params: Optional[dict[str, str]] = None,
    ) -> str:
        """
        Build authorization URL for OAuth flow.

        Args:
            state: CSRF protection state parameter
            code_challenge: PKCE code challenge
            extra_params: Additional query parameters

        Returns:
            Authorization URL

        Raises:
            RuntimeError: If discover() hasn't been called
        """
        if not self.authorization_endpoint:
            await self.discover()

        if not self.authorization_endpoint:
            raise RuntimeError("Authorization endpoint not discovered")

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        if extra_params:
            params.update(extra_params)

        return f"{self.authorization_endpoint}?{urlencode(params)}"

    async def exchange_authorization_code(
        self,
        code: str,
        code_verifier: str,
    ) -> dict:
        """
        Exchange authorization code for tokens.

        Args:
            code: Authorization code from OAuth callback
            code_verifier: PKCE code verifier

        Returns:
            Token response dictionary with keys:
                - access_token: Access token
                - refresh_token: Refresh token (if offline_access scope requested)
                - id_token: ID token (JWT)
                - expires_in: Access token lifetime in seconds
                - refresh_expires_in: Refresh token lifetime in seconds (optional)
                - token_type: Token type (Bearer)

        Raises:
            httpx.HTTPError: If token exchange fails
        """
        if not self.token_endpoint:
            await self.discover()

        if not self.token_endpoint:
            raise RuntimeError("Token endpoint not discovered")

        logger.debug(
            f"Exchanging authorization code for tokens at {self.token_endpoint}"
        )

        client = await self._get_http_client()
        response = await client.post(
            self.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": code_verifier,
            },
            auth=(self.client_id, self.client_secret),
        )

        response.raise_for_status()
        token_data = response.json()

        logger.info("✓ Successfully exchanged authorization code for tokens")

        if "refresh_token" in token_data:
            logger.info("  Received refresh token (offline_access granted)")

        return token_data

    async def refresh_access_token(self, refresh_token: str) -> dict:
        """
        Refresh access token using refresh token.

        Args:
            refresh_token: Refresh token

        Returns:
            Token response dictionary (same format as exchange_authorization_code)

        Raises:
            httpx.HTTPError: If token refresh fails
        """
        if not self.token_endpoint:
            await self.discover()

        if not self.token_endpoint:
            raise RuntimeError("Token endpoint not discovered")

        logger.debug("Refreshing access token")

        client = await self._get_http_client()
        response = await client.post(
            self.token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            auth=(self.client_id, self.client_secret),
        )

        response.raise_for_status()
        token_data = response.json()

        logger.debug("✓ Successfully refreshed access token")

        return token_data

    async def get_userinfo(self, access_token: str) -> dict:
        """
        Get user information using access token.

        Args:
            access_token: Access token

        Returns:
            Userinfo response dictionary with claims like:
                - sub: Subject (user ID)
                - name: Full name
                - preferred_username: Username
                - email: Email address
                - email_verified: Email verification status

        Raises:
            httpx.HTTPError: If userinfo request fails
        """
        if not self.userinfo_endpoint:
            await self.discover()

        if not self.userinfo_endpoint:
            raise RuntimeError("Userinfo endpoint not discovered")

        logger.debug("Fetching user info")

        client = await self._get_http_client()
        response = await client.get(
            self.userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )

        response.raise_for_status()
        userinfo = response.json()

        logger.debug(f"✓ Retrieved user info for subject: {userinfo.get('sub')}")

        return userinfo

    async def check_token_exchange_support(self) -> bool:
        """
        Check if Keycloak supports RFC 8693 token exchange.

        Returns:
            True if token exchange is supported

        Note:
            This is ADR-002 Tier 2. Most Keycloak installations don't
            have token exchange enabled by default.
        """
        if not self.token_endpoint:
            await self.discover()

        # Try to get discovery document and check for token exchange grant
        discovery_url = (
            f"{self.keycloak_url}/realms/{self.realm}/.well-known/openid-configuration"
        )

        try:
            client = await self._get_http_client()
            response = await client.get(discovery_url)
            response.raise_for_status()
            discovery_data = response.json()

            grant_types = discovery_data.get("grant_types_supported", [])
            supported = "urn:ietf:params:oauth:grant-type:token-exchange" in grant_types

            if supported:
                logger.info("✓ Token exchange (RFC 8693) is supported")
            else:
                logger.info("Token exchange (RFC 8693) is not supported")

            return supported

        except Exception as e:
            logger.warning(f"Failed to check token exchange support: {e}")
            return False


__all__ = ["KeycloakOAuthClient", "generate_state", "verify_state"]
