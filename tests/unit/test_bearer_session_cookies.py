"""Unit tests for per-token Nextcloud session cookies in bearer pass-through.

Without a session cookie Nextcloud runs a full login for every bearer request
(a 4096-bit RSA key and a stored ``oc_authtoken`` row each time). These tests
pin the three things that make the cookie reuse real and safe:

* a ``Secure`` cookie is replayed over plain http to the in-cluster hostname
  (the stock httpx jar stores it and never sends it);
* one token's cookies are never sent with another token;
* the store is bounded and its entries expire.
"""

import base64
import json
import secrets
import time
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest

from nextcloud_mcp_server import client as client_mod
from nextcloud_mcp_server import context as ctx_mod
from nextcloud_mcp_server import session_cookies as sc
from nextcloud_mcp_server.auth import grant_ownership as go
from nextcloud_mcp_server.auth.bearer_auth import BearerAuth
from nextcloud_mcp_server.client import AsyncDisableCookieTransport

pytestmark = pytest.mark.unit

# Plain http and not loopback, like the in-cluster Service URL the server is
# deployed against. No request leaves the process: every transport is mocked.
HOST = "http://nextcloud.nextcloud.svc:8080"
SESSION_COOKIE = "oc_sessionPassphrase"


class _Settings:
    nextcloud_host = HOST
    enable_multi_user_basic_auth = True
    enable_login_flow = False


class _FakeNextcloud:
    """Logs a bearer in when it arrives without its session cookie.

    Sets cookies the way Nextcloud does with ``overwriteprotocol=https``:
    every one carries ``Secure``.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.logins: list[str] = []
        self._sessions: dict[str, str] = {}  # session id -> Authorization

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        authorization = request.headers.get("Authorization", "")
        sent = httpx.Cookies()
        for part in request.headers.get("Cookie", "").split("; "):
            name, _, value = part.partition("=")
            if name:
                sent.set(name, value)

        headers: list[tuple[str, str]] = []
        if self._sessions.get(sent.get(SESSION_COOKIE) or "") != authorization:
            session_id = secrets.token_hex(8)
            self._sessions[session_id] = authorization
            self.logins.append(authorization)
            headers = [
                (
                    "set-cookie",
                    f"{SESSION_COOKIE}={session_id}; path=/; secure; HttpOnly; "
                    "SameSite=Lax",
                ),
                (
                    "set-cookie",
                    "__Host-nc_sameSiteCookielax=true; path=/; httponly;secure; "
                    "expires=Fri, 31-Dec-2100 23:59:59 GMT; SameSite=lax",
                ),
            ]
        return httpx.Response(
            200,
            headers=headers,
            json={"ocs": {"meta": {"statuscode": 200}, "data": {"id": "alice"}}},
        )

    def session_ids_sent_with(self, token: str) -> set[str]:
        ids = set()
        for request in self.requests:
            if request.headers.get("Authorization") != f"Bearer {token}":
                continue
            for part in request.headers.get("Cookie", "").split("; "):
                name, _, value = part.partition("=")
                if name == SESSION_COOKIE:
                    ids.add(value)
        return ids


@pytest.fixture
def nextcloud(monkeypatch) -> _FakeNextcloud:
    """Route every Nextcloud transport the bearer path builds to a fake."""
    fake = _FakeNextcloud()
    monkeypatch.setattr(ctx_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(go, "get_settings", lambda: _Settings())
    for module in (client_mod, go):
        monkeypatch.setattr(
            module, "nextcloud_httpx_transport", lambda: httpx.MockTransport(fake)
        )
    ctx_mod._bearer_uid_cache.clear()
    ctx_mod._bearer_session_cookies.clear()
    yield fake
    ctx_mod._bearer_uid_cache.clear()
    ctx_mod._bearer_session_cookies.clear()


def _ctx(state: dict[str, Any]) -> Any:
    request = SimpleNamespace(scope={"state": state})
    return SimpleNamespace(
        request_context=SimpleNamespace(request=request, lifespan_context=None)
    )


def _jwt(exp: float | None) -> str:
    """An unsigned JWT-shaped token; the signature part is random."""

    def segment(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    claims: dict[str, Any] = {"sub": secrets.token_hex(4)}
    if exp is not None:
        claims["exp"] = exp
    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.{secrets.token_urlsafe(16)}"


async def _mcp_request(token: str) -> None:
    """One MCP tool call: build the caller's client, make one API call."""
    client = await ctx_mod.get_client(_ctx({"bearer_token": token}))
    try:
        response = await client._client.get("/ocs/v2.php/apps/notifications/api/v2")
        assert response.status_code == 200
    finally:
        await client._client.aclose()


# --- Secure cookies over plain http -----------------------------------------


async def test_stock_httpx_jar_never_replays_secure_cookie_over_http():
    """The trap this change exists for, kept as a control.

    A client-level jar looks like it works (the cookie is stored) but
    ``http.cookiejar`` will not return a Secure cookie for an http:// URL, so
    Nextcloud sees every request as a new login. If this ever starts failing,
    httpx changed and the transport may no longer be needed.
    """
    fake = _FakeNextcloud()
    async with httpx.AsyncClient(
        base_url=HOST, transport=httpx.MockTransport(fake), auth=BearerAuth("t")
    ) as client:
        for _ in range(3):
            await client.get("/ocs/v2.php/cloud/user")

        assert SESSION_COOKIE in client.cookies
    assert len(fake.logins) == 3
    assert all("Cookie" not in r.headers for r in fake.requests)


async def test_transport_replays_secure_cookie_over_http():
    fake = _FakeNextcloud()
    token = secrets.token_urlsafe(32)
    transport = sc.AsyncSessionCookieTransport(
        httpx.MockTransport(fake), sc.new_session_cookies()
    )
    async with httpx.AsyncClient(
        base_url=HOST, transport=transport, auth=BearerAuth(token)
    ) as client:
        for _ in range(3):
            await client.get("/ocs/v2.php/cloud/user")

        # Set-Cookie is consumed by the transport; the client's jar stays empty.
        assert len(client.cookies.jar) == 0

    assert len(fake.logins) == 1
    assert "Cookie" not in fake.requests[0].headers
    for request in fake.requests[1:]:
        assert SESSION_COOKIE in request.headers["Cookie"]
        assert "__Host-nc_sameSiteCookielax=true" in request.headers["Cookie"]
    # The session never replaces the credential.
    assert all(r.headers["Authorization"] == f"Bearer {token}" for r in fake.requests)


async def test_bearer_requests_share_one_nextcloud_session(nextcloud):
    token = secrets.token_urlsafe(32)

    for _ in range(4):
        await _mcp_request(token)

    # 1 identity lookup + 4 API calls, from 4 separate clients: one login.
    assert len(nextcloud.requests) == 5
    assert len(nextcloud.logins) == 1
    assert all(
        r.headers["Authorization"] == f"Bearer {token}" for r in nextcloud.requests
    )
    assert all(r.url.scheme == "http" for r in nextcloud.requests)


# --- isolation ----------------------------------------------------------------


async def test_each_token_only_ever_sends_its_own_session(nextcloud):
    alice, bob = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    for _ in range(3):
        await _mcp_request(alice)
        await _mcp_request(bob)

    assert len(nextcloud.logins) == 2
    alice_ids = nextcloud.session_ids_sent_with(alice)
    bob_ids = nextcloud.session_ids_sent_with(bob)
    assert len(alice_ids) == 1
    assert len(bob_ids) == 1
    assert alice_ids.isdisjoint(bob_ids)


async def test_rotated_token_starts_a_new_session(nextcloud):
    old, new = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    await _mcp_request(old)
    await _mcp_request(new)

    assert len(nextcloud.logins) == 2
    assert nextcloud.session_ids_sent_with(new).isdisjoint(
        nextcloud.session_ids_sent_with(old)
    )


async def test_basic_auth_client_still_drops_cookies(nextcloud):
    password = secrets.token_urlsafe(16)

    client = await ctx_mod.get_client(
        _ctx({"basic_auth": {"username": "bob", "password": password}})
    )

    assert isinstance(client._client._transport, AsyncDisableCookieTransport)
    assert len(ctx_mod._bearer_session_cookies) == 0


async def test_concurrent_requests_for_one_token_share_one_jar(nextcloud):
    token = secrets.token_urlsafe(32)

    async with anyio.create_task_group() as tg:
        for _ in range(8):
            tg.start_soon(_mcp_request, token)

    assert len(ctx_mod._bearer_session_cookies) == 1
    assert len(nextcloud.session_ids_sent_with(token)) == 1


# --- the store ----------------------------------------------------------------


def test_store_keys_on_a_fingerprint_not_the_token():
    store = sc.SessionCookieStore()
    token = secrets.token_urlsafe(32)

    jar = store.get(token)

    assert store.get(token) is jar
    assert token not in store
    assert store.fingerprint(token) in store
    assert token not in repr(vars(store))


def test_store_evicts_least_recently_used_at_the_cap():
    store = sc.SessionCookieStore(max_entries=3)
    tokens = [secrets.token_urlsafe(16) for _ in range(4)]
    jars = [store.get(t) for t in tokens[:3]]

    store.get(tokens[0])  # touch: tokens[1] is now the oldest
    store.get(tokens[3])

    assert len(store) == 3
    assert store.fingerprint(tokens[1]) not in store
    assert store.get(tokens[0]) is jars[0]
    assert store.get(tokens[2]) is jars[2]


def test_store_entry_expires_after_ttl_even_if_used(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sc.time, "monotonic", lambda: clock[0])
    store = sc.SessionCookieStore(ttl=60.0)
    token = _jwt(exp=time.time() + 3600)

    jar = store.get(token)
    clock[0] += 59
    assert store.get(token) is jar  # using it does not extend it
    clock[0] += 2

    assert store.get(token) is not jar
    assert len(store) == 1


def test_store_drops_expired_entries_when_adding(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sc.time, "monotonic", lambda: clock[0])
    store = sc.SessionCookieStore(ttl_unknown_expiry=60.0)
    for _ in range(5):
        store.get(secrets.token_urlsafe(16))
    clock[0] += 61

    store.get(secrets.token_urlsafe(16))

    assert len(store) == 1


def test_store_entry_does_not_outlive_the_tokens_own_expiry(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sc.time, "monotonic", lambda: clock[0])
    store = sc.SessionCookieStore(ttl=3600.0)
    token = _jwt(exp=time.time() + 120)

    jar = store.get(token)
    clock[0] += 100
    assert store.get(token) is jar
    clock[0] += 30

    assert store.get(token) is not jar


def test_store_does_not_keep_a_jar_for_an_expired_token():
    store = sc.SessionCookieStore()

    store.get(_jwt(exp=time.time() - 5))

    assert len(store) == 0


@pytest.mark.parametrize(
    "token",
    [
        "opaque-token",
        "a.b.c",
        "e30.eyJzdWIiOiAiYWxpY2UifQ.sig",  # a JWT with no exp
        "e30.WyJub3QiLCJhIiwiZGljdCJd.sig",  # payload is a JSON list
        "e30.eyJleHAiOiAic29vbiJ9.sig",  # exp is a string
    ],
)
def test_token_without_readable_expiry_gets_the_short_ttl(monkeypatch, token):
    clock = [1000.0]
    monkeypatch.setattr(sc.time, "monotonic", lambda: clock[0])
    store = sc.SessionCookieStore(ttl=3600.0, ttl_unknown_expiry=60.0)

    jar = store.get(token)
    clock[0] += 61

    assert store.get(token) is not jar
