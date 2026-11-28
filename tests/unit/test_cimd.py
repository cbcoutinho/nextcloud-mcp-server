"""CIMD client_ids and RFC 9207 ``iss`` on the OAuth AS proxy (GH #1470)."""

import ipaddress
import json
import string
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx2
import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st

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
from tests.strategies import json_values

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


_SEGMENTS = st.text(
    alphabet=string.ascii_letters + string.digits + "-_~.", min_size=1, max_size=8
).filter(lambda s: s not in (".", ".."))
_DOTS = st.sampled_from([".", "%2e", "%2E"])  # a dot, raw or percent-encoded
_MUTATIONS = ["scheme", "userinfo", "host", "port", "no_path", "dot_segment"]


@st.composite
def client_id_urls(draw):
    """``(client_id, valid)``: a valid client_id URL, or one broken by one rule.

    Host case and port vary in the valid URLs: both are legal and the host is
    compared case-insensitively. A fragment has its own property below.

    The values every mutation needs are drawn whichever one applies, so the
    number of draws is fixed: Hypothesis's mutations never outgrow the example
    and discard it.
    """
    host = "".join(c.upper() if draw(st.booleans()) else c for c in "client.example")
    host += ".com"
    port_number = draw(st.integers(1, 65535))
    port = f":{port_number}" if draw(st.booleans()) else ""
    n_segments = draw(st.integers(1, 4))
    segments = [draw(_SEGMENTS) for _ in range(n_segments)]
    path = "/" + "/".join(segments)

    mutation = draw(st.sampled_from([None, *_MUTATIONS]))
    event(f"mutation: {mutation}")
    bad_scheme = draw(st.sampled_from(["http", "ftp"]))
    userinfo = draw(st.sampled_from(["user@", "user:pw@", "@", ":@"]))
    bad_host = draw(
        st.sampled_from(
            [
                "untrusted.example.com",
                "client.example.com.evil.example",
                "evilclient.example.com",
                "example.com",
            ]
        )
    )
    bad_port = draw(
        st.text(string.ascii_letters, min_size=1, max_size=4)
        | st.integers(65536, 10**6).map(str)
    )
    bare_path = draw(st.sampled_from(["", "/"]))
    dots = [draw(_DOTS), draw(_DOTS)]
    dot = "".join(dots[: draw(st.sampled_from([1, 2]))])
    dot_at = draw(st.integers(0, len(segments)))

    scheme = bad_scheme if mutation == "scheme" else "https"
    if mutation != "userinfo":
        userinfo = ""
    if mutation == "host":
        host = bad_host
    elif mutation == "port":
        port = ":" + bad_port
    elif mutation == "no_path":
        path = bare_path
    elif mutation == "dot_segment":
        segments.insert(dot_at, dot)
        path = "/" + "/".join(segments)
    return f"{scheme}://{userinfo}{host}{port}{path}", mutation is None


@given(client_id_urls())
@example(("https://client.example.com/a/%2e%2e/metadata.json", False))
@example(("https://client.example.com/a/%2E/metadata.json", False))
async def test_client_id_url_is_accepted_exactly_when_valid(case):
    """Draft §3 plus the host allowlist; a rejection never reaches the network."""
    client_id, valid = case
    cimd._cache.clear()
    document = {**DOCUMENT, "client_id": client_id}
    fetch = AsyncMock(return_value=json.dumps(document).encode())
    with patch.object(cimd, "_fetch", new=fetch):
        if not valid:
            with pytest.raises(cimd.CIMDError):
                await cimd.get_client_metadata(client_id)
            fetch.assert_not_called()
            return
        assert await cimd.get_client_metadata(client_id) == document
    fetch.assert_awaited_once()


@given(st.text(string.ascii_letters + string.digits, max_size=6))
@example("frag")
@example("")  # urlsplit drops an empty fragment, so it once slipped through
async def test_a_client_id_with_a_fragment_is_rejected(fragment):
    client_id = f"{CLIENT_ID}#{fragment}"
    with patch.object(cimd, "_fetch", new=AsyncMock()) as fetch:
        with pytest.raises(cimd.CIMDError):
            await cimd.get_client_metadata(client_id)
    fetch.assert_not_called()


# Non-public ranges an SSRF would aim at: loopback, RFC 1918, link-local (cloud
# metadata at 169.254.169.254), CGNAT, "this network", ULA.
_NON_PUBLIC_NETWORKS = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "0.0.0.0/8",
    "::1/128",
    "fc00::/7",
]
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _nat64(v4: ipaddress.IPv4Address) -> str:
    return str(ipaddress.IPv6Address(int(_NAT64.network_address) | int(v4)))


@given(
    st.sampled_from(_NON_PUBLIC_NETWORKS).flatmap(
        lambda net: st.ip_addresses(network=net)
    ),
    st.booleans(),
)
@example(ipaddress.ip_address("169.254.169.254"), False)
# The NAT64 well-known prefix (RFC 6052): ipaddress calls the whole /96 global,
# but a DNS64/NAT64 path translates these straight back to the embedded IPv4.
@example(ipaddress.ip_address("127.0.0.1"), True)
@example(ipaddress.ip_address("10.1.2.3"), True)
async def test_non_public_address_is_refused(address, via_nat64):
    host = _nat64(address) if via_nat64 and address.version == 4 else str(address)
    with pytest.raises(cimd.CIMDError, match="not publicly routable"):
        await cimd._resolve_public_address(host, 443)


@given(st.ip_addresses(v=4))
def test_nat64_address_is_routable_exactly_when_its_ipv4_is(v4):
    assert cimd._is_globally_routable(_nat64(v4)) == cimd._is_globally_routable(str(v4))


@given(
    st.binary()
    | json_values.map(lambda v: json.dumps(v).encode())
    | st.dictionaries(
        st.sampled_from(
            [
                "client_id",
                "redirect_uris",
                "client_secret",
                "token_endpoint_auth_method",
            ]
        ),
        json_values | st.just(CLIENT_ID) | st.lists(st.text()),
    ).map(lambda v: json.dumps(v).encode())
    # Well-formed documents, so the accepting branch is reached too.
    | st.fixed_dictionaries(
        {
            "client_id": st.just(CLIENT_ID),
            "redirect_uris": st.lists(st.text(), min_size=1),
        },
        optional={"client_name": json_values, "token_endpoint_auth_method": st.text()},
    ).map(lambda v: json.dumps(v).encode())
)
@example(b"\xff\xfe")  # not UTF-8
def test_validate_document_raises_only_cimd_error(body):
    """The document is attacker-controlled bytes: any failure is a CIMDError."""
    try:
        document = cimd._validate_document(CLIENT_ID, body)
    except cimd.CIMDError:
        event("rejected")
        return
    event("accepted")
    assert document["client_id"] == CLIENT_ID
    uris = document["redirect_uris"]
    assert uris and all(isinstance(u, str) for u in uris)


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


LOOPBACK_DOCUMENT = {
    "client_id": CLIENT_ID,
    "redirect_uris": [
        "http://localhost/callback",
        "http://127.0.0.1/callback",
        "http://[::1]/callback",
        "http://localhost:3000/fixed",
        REDIRECT,
    ],
}


_LOOPBACK_REGISTERED = LOOPBACK_DOCUMENT["redirect_uris"]
_hosts = st.sampled_from(["localhost", "127.0.0.1", "[::1]"])
_ports = st.integers(min_value=1, max_value=65535)


async def test_an_ephemeral_loopback_port_passes_validation():
    """Wiring: validate_cimd_client applies the matcher below to the document."""
    fetch = AsyncMock(return_value=json.dumps(LOOPBACK_DOCUMENT).encode())
    with patch.object(cimd, "_fetch", new=fetch):
        assert (
            await cimd.validate_cimd_client(
                CLIENT_ID, "http://localhost:54321/callback"
            )
            is None
        )


@given(_hosts, st.none() | _ports)
@example("[::1]", 61000)
def test_portless_loopback_redirect_matches_any_port(host, port):
    """RFC 8252 §7.3: native clients listen on an ephemeral port."""
    uri = f"http://{host}" + (f":{port}" if port else "") + "/callback"
    assert cimd._redirect_uri_matches(uri, _LOOPBACK_REGISTERED)


@given(
    _hosts,
    _ports,
    st.sampled_from(
        [
            "http://{h}:{p}/other",  # different path
            "http://{h}:{p}/callback?x=1",  # different query
            "http://{h}:{p}/callback#frag",
            "https://{h}:{p}/callback",  # different scheme
            "http://user@{h}:{p}/callback",
            "http://{h}.evil.example:{p}/callback",
            "http://example.com:{p}/callback",  # not loopback
            "https://client.example.com:{p}/callback",  # only loopback relaxed
            "http://{h}:{p}/callback#",  # even an empty fragment
            "http://{h}:7{p:05d}/callback",  # out of range
            "http://{h}:x{p}/callback",
        ]
    ),
)
@example("localhost", 19, "http://{h}:{p}/callback#")  # empty fragment slipped through
def test_loopback_port_relaxation_is_narrow(host, port, template):
    """Only the port is relaxed; every other component must still match."""
    uri = template.format(h=host, p=port)
    assert not cimd._redirect_uri_matches(uri, _LOOPBACK_REGISTERED)


@given(_ports.filter(lambda p: p != 3000))
def test_a_registered_port_matches_exactly(port):
    assert cimd._redirect_uri_matches(
        "http://localhost:3000/fixed", _LOOPBACK_REGISTERED
    )
    assert not cimd._redirect_uri_matches(
        f"http://localhost:{port}/fixed", _LOOPBACK_REGISTERED
    )


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
        new=AsyncMock(side_effect=httpx2.ConnectError("idp unreachable")),
    ):
        response = await oauth_as_metadata(_metadata_request("https://idp/.wk"))

    metadata = json.loads(bytes(response.body))
    assert metadata["registration_endpoint"] == "https://mcp.example.com/oauth/register"
