"""Unit tests for the startup OIDC discovery retry/backoff in app.py.

Startup discovery runs synchronously and is fatal on failure. On a freshly
scheduled pod the egress path (Cilium toFQDN allow + egress-gateway SNAT
programming) can take a few seconds to converge, during which the request is
dropped and times out. ``_perform_oidc_discovery`` retries transient failures
with capped exponential backoff so a cold-start race doesn't crashloop the
backend (see the crashloop investigation for tenant-ergro).
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nextcloud_mcp_server.app import _perform_oidc_discovery
from nextcloud_mcp_server.config import Settings

pytestmark = pytest.mark.unit

DISCOVERY_URL = "https://nx.example.com/.well-known/openid-configuration"
DISCOVERY_DOC = {"issuer": "https://nx.example.com"}


def _settings(
    max_attempts: int = 5,
    backoff_base: float = 0.0,
    backoff_max: float = 0.0,
) -> Settings:
    """Settings with fast, deterministic backoff for tests."""
    return Settings(
        oidc_discovery_max_attempts=max_attempts,
        oidc_discovery_backoff_base=backoff_base,
        oidc_discovery_backoff_max=backoff_max,
    )


def _patched_client(handler):
    """Patch app.nextcloud_httpx_client to route through a MockTransport."""
    transport = httpx.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    return patch(
        "nextcloud_mcp_server.app.nextcloud_httpx_client",
        side_effect=fake_client,
    )


async def test_returns_document_on_first_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=DISCOVERY_DOC)

    with _patched_client(handler):
        with patch("nextcloud_mcp_server.app.anyio.sleep", AsyncMock()) as sleep:
            result = await _perform_oidc_discovery(DISCOVERY_URL, _settings())

    assert result == DISCOVERY_DOC
    sleep.assert_not_awaited()  # no retries on immediate success


async def test_retries_transient_connect_timeout_then_succeeds():
    """A ConnectTimeout (the tenant-ergro cold-start symptom) is retried."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectTimeout("egress not ready", request=request)
        return httpx.Response(200, json=DISCOVERY_DOC)

    with _patched_client(handler):
        with patch("nextcloud_mcp_server.app.anyio.sleep", AsyncMock()) as sleep:
            result = await _perform_oidc_discovery(DISCOVERY_URL, _settings())

    assert result == DISCOVERY_DOC
    assert attempts["n"] == 3
    assert sleep.await_count == 2  # slept before each of the 2 retries


async def test_retries_5xx_then_succeeds():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(200, json=DISCOVERY_DOC)

    with _patched_client(handler):
        with patch("nextcloud_mcp_server.app.anyio.sleep", AsyncMock()):
            result = await _perform_oidc_discovery(DISCOVERY_URL, _settings())

    assert result == DISCOVERY_DOC
    assert attempts["n"] == 2


async def test_4xx_raises_immediately_without_retry():
    """A 4xx is a misconfiguration, not a transient error — fail fast."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404)

    with _patched_client(handler):
        with patch("nextcloud_mcp_server.app.anyio.sleep", AsyncMock()) as sleep:
            with pytest.raises(httpx.HTTPStatusError):
                await _perform_oidc_discovery(DISCOVERY_URL, _settings())

    assert attempts["n"] == 1
    sleep.assert_not_awaited()


async def test_raises_after_exhausting_attempts():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectTimeout("still not ready", request=request)

    with _patched_client(handler):
        with patch("nextcloud_mcp_server.app.anyio.sleep", AsyncMock()) as sleep:
            with pytest.raises(httpx.ConnectTimeout):
                await _perform_oidc_discovery(DISCOVERY_URL, _settings(max_attempts=3))

    assert attempts["n"] == 3
    assert sleep.await_count == 2  # no sleep after the final failed attempt


async def test_max_attempts_one_restores_fail_fast():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectTimeout("boom", request=request)

    with _patched_client(handler):
        with patch("nextcloud_mcp_server.app.anyio.sleep", AsyncMock()) as sleep:
            with pytest.raises(httpx.ConnectTimeout):
                await _perform_oidc_discovery(DISCOVERY_URL, _settings(max_attempts=1))

    assert attempts["n"] == 1
    sleep.assert_not_awaited()
