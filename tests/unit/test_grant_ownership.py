"""Unit tests for the Login Flow v2 grant-ownership check (GHSA-84qv-22q6-x82r).

HTTP is exercised via an ``httpx.MockTransport`` injected by monkeypatching
``httpx.AsyncClient`` (the repo has no respx dependency).
"""

import secrets
from typing import Any

import httpx
import pytest

from nextcloud_mcp_server.auth import grant_ownership as go

pytestmark = pytest.mark.unit

# Generated, not a hardcoded literal — keeps this a fake token, not a
# credential pattern (SonarQube python:S2068).
APP_PASSWORD = secrets.token_urlsafe(24)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """Point the helper at a fixed Nextcloud host and clear the endpoint cache."""

    class _S:
        nextcloud_host = "https://cloud.example.com"
        oidc_discovery_url = None

    monkeypatch.setattr(go, "get_settings", lambda: _S())
    go._userinfo_endpoints.clear()
    yield
    go._userinfo_endpoints.clear()


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


# ── grant_belongs_to_caller ──


async def test_accepts_grant_from_the_caller(monkeypatch):
    _patch_transport(monkeypatch, lambda _r: _ocs_user("alice"))

    assert await go.grant_belongs_to_caller({"alice"}, "alice", APP_PASSWORD) is True


async def test_accepts_login_name_that_differs_from_the_uid(monkeypatch):
    """Login-by-email and LDAP logins resolve through OCS to the caller's UID."""
    _patch_transport(monkeypatch, lambda _r: _ocs_user("alice"))

    assert (
        await go.grant_belongs_to_caller({"alice"}, "alice@example.com", APP_PASSWORD)
        is True
    )


async def test_rejects_grant_from_another_account(monkeypatch):
    """The advisory's attack: bob starts the flow, admin completes it."""
    _patch_transport(monkeypatch, lambda _r: _ocs_user("admin"))

    assert await go.grant_belongs_to_caller({"bob"}, "admin", APP_PASSWORD) is False


async def test_rejects_when_the_credential_does_not_authenticate(monkeypatch):
    _patch_transport(monkeypatch, lambda _r: httpx.Response(401))

    assert await go.grant_belongs_to_caller({"alice"}, "alice", APP_PASSWORD) is False


async def test_rejects_when_nextcloud_is_unreachable(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _patch_transport(monkeypatch, handler)

    assert await go.grant_belongs_to_caller({"alice"}, "alice", APP_PASSWORD) is False


async def test_rejects_grant_without_a_login_name(monkeypatch):
    _patch_transport(monkeypatch, lambda _r: _ocs_user("alice"))

    assert await go.grant_belongs_to_caller({"alice"}, None, APP_PASSWORD) is False


# ── caller_identities ──


async def test_caller_identities_without_a_token_is_just_the_sub():
    assert await go.caller_identities("Alice") == {"alice"}


async def test_caller_identities_adds_preferred_username_from_the_idp(monkeypatch):
    """External IdP: ``sub`` is a UUID, the Nextcloud account is preferred_username."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200, json={"userinfo_endpoint": "https://idp.example.com/userinfo"}
            )
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"sub": "uuid-1", "preferred_username": "Bob"})

    _patch_transport(monkeypatch, handler)

    assert await go.caller_identities("uuid-1", "tok") == {"uuid-1", "bob"}


async def test_external_idp_caller_owns_their_grant(monkeypatch):
    """End to end for the Keycloak shape: UUID sub, Nextcloud UID from the IdP."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200, json={"userinfo_endpoint": "https://idp.example.com/userinfo"}
            )
        if request.url.path.endswith("/userinfo"):
            return httpx.Response(200, json={"preferred_username": "bob"})
        return _ocs_user("bob")

    _patch_transport(monkeypatch, handler)

    identities = await go.caller_identities("6f1c-uuid", "tok")
    assert await go.grant_belongs_to_caller(identities, "bob", APP_PASSWORD) is True


async def test_caller_identities_survives_an_unreachable_idp(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _patch_transport(monkeypatch, handler)

    assert await go.caller_identities("alice", "tok") == {"alice"}


# ── revoke_app_password ──


async def test_revoke_deletes_the_app_password(monkeypatch):
    seen = _patch_transport(monkeypatch, lambda _r: httpx.Response(200))

    await go.revoke_app_password("admin", APP_PASSWORD)

    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/ocs/v2.php/core/apppassword"


async def test_revoke_swallows_transport_errors(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _patch_transport(monkeypatch, handler)

    await go.revoke_app_password("admin", APP_PASSWORD)  # must not raise
