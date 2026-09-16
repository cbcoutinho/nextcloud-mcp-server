"""CIMD client_ids and RFC 9207 ``iss`` on the OAuth AS proxy (GH #1470)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest

import nextcloud_mcp_server.auth.client_registry as registry_mod
import nextcloud_mcp_server.auth.oauth_routes as oauth_routes
from nextcloud_mcp_server.auth import cimd
from nextcloud_mcp_server.auth.oauth_routes import (
    ASProxySession,
    _as_proxy_sessions,
    _oauth_callback_as_proxy,
    oauth_as_metadata,
    oauth_authorize,
)
from nextcloud_mcp_server.config import _reload_config

pytestmark = pytest.mark.unit

CLIENT_ID = "https://client.example.com/oauth/metadata.json"
REDIRECT = "https://client.example.com/callback"
DOCUMENT = {"client_id": CLIENT_ID, "client_name": "Ex", "redirect_uris": [REDIRECT]}


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    def configure(**env: str) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        _reload_config()
        registry_mod._registry = None

    monkeypatch.setenv("NEXTCLOUD_MCP_SERVER_URL", "https://mcp.example.com")
    monkeypatch.delenv("ALLOWED_MCP_CLIENTS", raising=False)
    configure(CIMD_ALLOWED_HOSTS="client.example.com")
    cimd._cache.clear()
    yield configure
    monkeypatch.undo()
    _reload_config()
    registry_mod._registry = None
    cimd._cache.clear()


@pytest.mark.parametrize(
    "client_id",
    [
        "https://client.example.com",  # no path
        "https://client.example.com/",
        "https://client.example.com/a/../metadata.json",
        "https://client.example.com/a/%2e%2e/metadata.json",
        "https://client.example.com/a/%2E/metadata.json",
        "https://client.example.com/metadata.json#frag",
        "https://user:pw@client.example.com/metadata.json",
        "https://client.example.com:abc/metadata.json",
        "https://untrusted.example.com/metadata.json",
    ],
)
async def test_invalid_or_untrusted_client_id_url_is_rejected(client_id):
    with patch.object(cimd, "_fetch", new=AsyncMock()) as fetch:
        with pytest.raises(cimd.CIMDError):
            await cimd.get_client_metadata(client_id)
    fetch.assert_not_called()


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "::1",
        # NAT64 well-known prefix (RFC 6052) encoding 127.0.0.1 and 10.1.2.3.
        # ipaddress calls the whole /96 global, but a DNS64/NAT64 path
        # translates these straight back to the embedded IPv4 address.
        "64:ff9b::7f00:1",
        "64:ff9b::a01:203",
    ],
)
async def test_non_public_address_is_refused(host):
    with pytest.raises(cimd.CIMDError, match="not publicly routable"):
        await cimd._resolve_public_address(host, 443)


@pytest.mark.parametrize(
    "document",
    [
        [DOCUMENT],
        {**DOCUMENT, "client_id": "https://client.example.com/other.json"},
        {**DOCUMENT, "redirect_uris": []},
        {**DOCUMENT, "redirect_uris": "https://client.example.com/callback"},
        {**DOCUMENT, "client_secret": "s3cret"},
        {**DOCUMENT, "token_endpoint_auth_method": "client_secret_basic"},
    ],
)
async def test_invalid_document_is_rejected_and_not_cached(document):
    fetch = AsyncMock(return_value=json.dumps(document).encode())
    with patch.object(cimd, "_fetch", new=fetch):
        assert await cimd.validate_cimd_client(CLIENT_ID, REDIRECT) is not None
        await cimd.validate_cimd_client(CLIENT_ID, REDIRECT)
    assert fetch.await_count == 2  # failures are never cached


async def test_valid_document_checks_redirect_uri_and_is_cached():
    fetch = AsyncMock(return_value=json.dumps(DOCUMENT).encode())
    with patch.object(cimd, "_fetch", new=fetch):
        assert await cimd.validate_cimd_client(CLIENT_ID, REDIRECT) is None
        assert "redirect_uri" in (
            await cimd.validate_cimd_client(CLIENT_ID, "https://evil.example.com/cb")
            or ""
        )
    fetch.assert_awaited_once()


async def test_slow_fetch_is_bounded_by_one_overall_deadline(monkeypatch):
    async def slow_resolve(host: str, port: int) -> str:
        await anyio.sleep(10)
        return "93.184.215.14"

    monkeypatch.setattr(cimd, "_FETCH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cimd, "_resolve_public_address", slow_resolve)
    with pytest.raises(cimd.CIMDError, match="timed out"):
        await cimd.get_client_metadata(CLIENT_ID)


def _authorize_request(client_id: str, redirect_uri: str) -> MagicMock:
    request = MagicMock()
    request.query_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": "client-state",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
    }
    request.app.state.oauth_context = {
        "config": {
            "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
            "mcp_server_url": "https://mcp.example.com",
            "client_id": "mcp-server",
            "nextcloud_host": "https://idp.example.com",
        }
    }
    return request


async def test_authorize_accepts_cimd_client_without_registration():
    discovery = {"authorization_endpoint": "https://idp.example.com/authorize"}
    fetch = AsyncMock(return_value=json.dumps(DOCUMENT).encode())
    with (
        patch.object(cimd, "_fetch", new=fetch),
        patch(
            "nextcloud_mcp_server.auth.oauth_routes.get_oidc_discovery",
            new=AsyncMock(return_value=discovery),
        ),
    ):
        ok = await oauth_authorize(_authorize_request(CLIENT_ID, REDIRECT))
        bad = await oauth_authorize(
            _authorize_request(CLIENT_ID, "https://evil.example.com/cb")
        )

    assert ok.status_code == 302
    assert ok.headers["location"].startswith("https://idp.example.com/authorize?")
    assert bad.status_code == 401


async def test_registered_client_wins_over_cimd_for_url_client_id(_config):
    """An explicitly allowlisted client is not re-routed through a document fetch."""
    _config(ALLOWED_MCP_CLIENTS=f"{CLIENT_ID}|{REDIRECT}")
    discovery = {"authorization_endpoint": "https://idp.example.com/authorize"}
    with (
        patch.object(cimd, "_fetch", new=AsyncMock()) as fetch,
        patch(
            "nextcloud_mcp_server.auth.oauth_routes.get_oidc_discovery",
            new=AsyncMock(return_value=discovery),
        ),
    ):
        response = await oauth_authorize(_authorize_request(CLIENT_ID, REDIRECT))

    fetch.assert_not_called()
    assert response.status_code == 302


async def test_cimd_authorize_is_rate_limited_per_ip():
    """The outbound document fetch is capped like the DCR proxy's."""
    oauth_routes._cimd_rate_limit.clear()
    fetch = AsyncMock(return_value=json.dumps(DOCUMENT).encode())
    request = _authorize_request(CLIENT_ID, REDIRECT)
    request.client.host = "203.0.113.7"
    discovery = {"authorization_endpoint": "https://idp.example.com/authorize"}

    try:
        with (
            patch.object(cimd, "_fetch", new=fetch),
            patch(
                "nextcloud_mcp_server.auth.oauth_routes.get_oidc_discovery",
                new=AsyncMock(return_value=discovery),
            ),
        ):
            allowed = [
                await oauth_authorize(request)
                for _ in range(oauth_routes._CIMD_RATE_LIMIT_MAX)
            ]
            blocked = await oauth_authorize(request)
    finally:
        oauth_routes._cimd_rate_limit.clear()

    assert {r.status_code for r in allowed} == {302}
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == str(oauth_routes._CIMD_RATE_LIMIT_WINDOW)
    assert json.loads(bytes(blocked.body))["error"] == "too_many_requests"


async def test_cimd_disabled_treats_url_client_id_as_unknown(_config):
    _config(CIMD_ALLOWED_HOSTS="")
    with patch.object(cimd, "_fetch", new=AsyncMock()) as fetch:
        response = await oauth_authorize(_authorize_request(CLIENT_ID, REDIRECT))
    fetch.assert_not_called()
    assert response.status_code == 401


async def test_authorization_error_response_carries_iss():
    _as_proxy_sessions["srv-state"] = ASProxySession(
        client_id=CLIENT_ID,
        client_redirect_uri=REDIRECT,
        client_state="client-state",
        code_challenge="challenge",
        code_challenge_method="S256",
        requested_scopes="openid",
        nonce="nonce",
    )
    request = MagicMock()
    request.query_params = {"error": "access_denied", "state": "srv-state"}

    response = await _oauth_callback_as_proxy(request, "srv-state")

    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["iss"] == ["https://mcp.example.com"]
    assert query["state"] == ["client-state"]
    assert query["error"] == ["access_denied"]


def _metadata_request(discovery_url: str | None) -> MagicMock:
    request = MagicMock()
    request.app.state.supported_scopes = None
    request.app.state.oauth_context = {"config": {"discovery_url": discovery_url}}
    return request


@pytest.mark.parametrize(
    ("idp_registration", "static_clients", "advertised"),
    [(None, "", False), ("https://idp/reg", "", True), (None, "claude-code", True)],
)
async def test_metadata_advertises_registration_only_when_it_can_work(
    _config, idp_registration, static_clients, advertised
):
    _config(ALLOWED_MCP_CLIENTS=static_clients)
    discovery = {"registration_endpoint": idp_registration}
    with patch(
        "nextcloud_mcp_server.auth.oauth_routes.get_oidc_discovery",
        new=AsyncMock(return_value=discovery),
    ):
        response = await oauth_as_metadata(_metadata_request("https://idp/.wk"))

    metadata = json.loads(bytes(response.body))
    assert ("registration_endpoint" in metadata) is advertised
    assert metadata["issuer"] == "https://mcp.example.com"
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert metadata["client_id_metadata_document_supported"] is True
    assert "none" in metadata["token_endpoint_auth_methods_supported"]


async def test_metadata_fails_open_when_discovery_is_unavailable(_config):
    """A discovery outage must not strip DCR from a deployment that has it."""
    _config(ALLOWED_MCP_CLIENTS="")
    with patch(
        "nextcloud_mcp_server.auth.oauth_routes.get_oidc_discovery",
        new=AsyncMock(side_effect=httpx.ConnectError("idp unreachable")),
    ):
        response = await oauth_as_metadata(_metadata_request("https://idp/.wk"))

    metadata = json.loads(bytes(response.body))
    assert metadata["registration_endpoint"] == "https://mcp.example.com/oauth/register"
