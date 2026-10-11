"""Unit tests for bearer pass-through in multi-user BasicAuth mode.

``get_client`` must hand an ``Authorization: Bearer`` token to Nextcloud
unchanged (Nextcloud validates it via user_oidc --check-bearer), resolve the
UID from Nextcloud's ``/cloud/user``, and leave the Basic path untouched.
"""

import secrets
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from httpx import BasicAuth

from nextcloud_mcp_server import context as ctx_mod
from nextcloud_mcp_server.auth import grant_ownership as go
from nextcloud_mcp_server.auth.bearer_auth import BearerAuth

pytestmark = pytest.mark.unit

HOST = "https://cloud.example.com"
# Generated so it is not a credential-shaped literal.
TOKEN = secrets.token_urlsafe(32)


class _Settings:
    nextcloud_host = HOST
    enable_multi_user_basic_auth = True
    enable_login_flow = False


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(ctx_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(go, "get_settings", lambda: _Settings())
    ctx_mod._bearer_uid_cache.clear()
    yield
    ctx_mod._bearer_uid_cache.clear()


def _ctx(state: dict[str, Any]) -> Any:
    request = SimpleNamespace(scope={"state": state})
    return SimpleNamespace(
        request_context=SimpleNamespace(request=request, lifespan_context=None)
    )


def _patch_transport(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        def _wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        kwargs["transport"] = httpx.MockTransport(_wrapped)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def _ocs_user(uid: str) -> httpx.Response:
    return httpx.Response(
        200, json={"ocs": {"meta": {"statuscode": 200}, "data": {"id": uid}}}
    )


async def test_bearer_builds_bearer_client_with_nextcloud_uid(monkeypatch):
    # user_oidc may hash the sub: the UID must come from Nextcloud, not the token.
    seen = _patch_transport(monkeypatch, lambda _r: _ocs_user("a1b2c3hashed"))

    client = await ctx_mod.get_client(_ctx({"bearer_token": TOKEN}))

    assert client.username == "a1b2c3hashed"
    assert isinstance(client._client.auth, BearerAuth)
    assert client._client.auth.token == TOKEN
    assert len(seen) == 1
    assert str(seen[0].url).startswith(f"{HOST}/ocs/v2.php/cloud/user")
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


async def test_bearer_rejected_by_nextcloud_raises(monkeypatch):
    _patch_transport(monkeypatch, lambda _r: httpx.Response(401))

    with pytest.raises(ValueError, match="not accepted by Nextcloud"):
        await ctx_mod.get_client(_ctx({"bearer_token": TOKEN}))
    assert ctx_mod._bearer_uid_cache == {}


async def test_bearer_uid_is_cached_per_token(monkeypatch):
    seen = _patch_transport(monkeypatch, lambda _r: _ocs_user("alice"))

    await ctx_mod.get_client(_ctx({"bearer_token": TOKEN}))
    await ctx_mod.get_client(_ctx({"bearer_token": TOKEN}))
    assert len(seen) == 1

    other = secrets.token_urlsafe(32)
    await ctx_mod.get_client(_ctx({"bearer_token": other}))
    assert len(seen) == 2
    # The raw token is never a cache key.
    assert TOKEN not in ctx_mod._bearer_uid_cache


async def test_bearer_cache_expires(monkeypatch):
    seen = _patch_transport(monkeypatch, lambda _r: _ocs_user("alice"))
    clock = [1000.0]
    monkeypatch.setattr(ctx_mod.time, "monotonic", lambda: clock[0])

    await ctx_mod.get_client(_ctx({"bearer_token": TOKEN}))
    clock[0] += ctx_mod._BEARER_UID_TTL_SECONDS + 1
    await ctx_mod.get_client(_ctx({"bearer_token": TOKEN}))

    assert len(seen) == 2


async def test_basic_auth_path_unchanged(monkeypatch):
    seen = _patch_transport(monkeypatch, lambda _r: _ocs_user("nobody"))
    password = secrets.token_urlsafe(16)

    client = await ctx_mod.get_client(
        _ctx({"basic_auth": {"username": "bob", "password": password}})
    )

    assert client.username == "bob"
    assert isinstance(client._client.auth, BasicAuth)
    assert seen == []  # no identity lookup on the Basic path


async def test_no_credentials_still_raises_basic_error():
    with pytest.raises(ValueError, match="BasicAuth credentials not found"):
        await ctx_mod.get_client(_ctx({}))


async def test_bearer_requires_nextcloud_host(monkeypatch):
    class _NoHost(_Settings):
        nextcloud_host = None

    monkeypatch.setattr(ctx_mod, "get_settings", lambda: _NoHost())

    with pytest.raises(ValueError, match="NEXTCLOUD_HOST"):
        await ctx_mod.get_client(_ctx({"bearer_token": TOKEN}))
