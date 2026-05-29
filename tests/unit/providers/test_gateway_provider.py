"""Gateway provider registration + M2M OIDC auth (design §10.2).

The gateway is manual-only: selected by EMBEDDING_PROVIDER=gateway and never by
the autodetect chain. Auth is the gateway's own M2M OIDC realm (parallel to the
tenant realm); creds are all-or-nothing.
"""

import time

import httpx
import pytest

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.embedding.gateway_client import (
    GatewayProvider,
    GatewayTokenProvider,
)
from nextcloud_mcp_server.providers.registry import ProviderRegistry, reset_provider
from nextcloud_mcp_server.providers.simple import SimpleProvider


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr(
        "nextcloud_mcp_server.providers.registry.get_settings", lambda: settings
    )
    reset_provider()


def test_gateway_selected_unauthenticated(monkeypatch):
    settings = Settings(
        embedding_provider="gateway",
        embedding_gateway_url="http://gateway:8083",
        embedding_gateway_model="mistral-embed",
    )
    _patch_settings(monkeypatch, settings)
    provider = ProviderRegistry.create_provider()
    assert isinstance(provider, GatewayProvider)
    assert provider.embedding_model == "mistral-embed"
    assert provider.supports_embeddings is True
    assert provider.supports_generation is False
    assert provider._token_provider is None  # unauthenticated


def test_gateway_selected_with_m2m_oidc(monkeypatch):
    settings = Settings(
        embedding_provider="gateway",
        embedding_gateway_url="http://gateway:8083",
        embedding_gateway_token_url="https://idp.example/oauth2/token",
        embedding_gateway_client_id="mcp-server",
        embedding_gateway_client_secret="shh",
        embedding_gateway_scope="astrolabe-embedding-gateway/embed",
    )
    _patch_settings(monkeypatch, settings)
    provider = ProviderRegistry.create_provider()
    assert isinstance(provider, GatewayProvider)
    assert isinstance(provider._token_provider, GatewayTokenProvider)


def test_partial_m2m_creds_rejected():
    with pytest.raises(ValueError, match="must be set together"):
        Settings(
            embedding_provider="gateway",
            embedding_gateway_url="http://gateway:8083",
            embedding_gateway_client_id="mcp-server",  # missing token_url/secret
        )


def test_autodetect_default_does_not_pick_gateway(monkeypatch):
    settings = Settings()
    _patch_settings(monkeypatch, settings)
    assert isinstance(ProviderRegistry.create_provider(), SimpleProvider)


def test_openai_creds_do_not_trigger_gateway(monkeypatch):
    settings = Settings(openai_api_key="sk-test")
    _patch_settings(monkeypatch, settings)
    assert not isinstance(ProviderRegistry.create_provider(), GatewayProvider)


async def test_token_provider_caches_and_refreshes(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.headers["Authorization"].startswith("Basic ")
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["grant_type"] == "client_credentials"
        assert body["scope"] == "embed"
        return httpx.Response(
            200, json={"access_token": f"tok{calls['n']}", "expires_in": 3600}
        )

    transport = httpx.MockTransport(handler)
    orig_async_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    tp = GatewayTokenProvider(
        token_url="https://idp.example/oauth2/token",
        client_id="cid",
        client_secret="sec",
        scope="embed",
    )
    t1 = await tp.get_token()
    t2 = await tp.get_token()  # cached → no new HTTP call
    assert t1 == t2 == "tok1"
    assert calls["n"] == 1

    # Expire the cache → next call refreshes.
    tp._cache = (tp._cache[0], time.time() - 1)
    t3 = await tp.get_token()
    assert t3 == "tok2"
    assert calls["n"] == 2
