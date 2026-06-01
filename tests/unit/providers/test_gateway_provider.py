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
        embedding_gateway_url="https://gateway:8083",
        embedding_gateway_model="mistral/mistral-embed",
    )
    _patch_settings(monkeypatch, settings)
    provider = ProviderRegistry.create_provider()
    assert isinstance(provider, GatewayProvider)
    assert provider.embedding_model == "mistral/mistral-embed"
    assert provider.supports_embeddings is True
    assert provider.supports_generation is False
    assert provider._token_provider is None  # unauthenticated


def test_gateway_selected_with_m2m_oidc(monkeypatch):
    settings = Settings(
        embedding_provider="gateway",
        embedding_gateway_url="https://gateway:8083",
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
            embedding_gateway_url="https://gateway:8083",
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
    assert tp._cache is not None
    tp._cache = (tp._cache[0], time.time() - 1)
    t3 = await tp.get_token()
    assert t3 == "tok2"
    assert calls["n"] == 2


async def test_token_provider_concurrent_callers_issue_single_request(monkeypatch):
    """Two concurrent get_token() calls must share one token request, not race."""
    import anyio

    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Hold the "network" open so a second caller arrives mid-flight.
        await anyio.sleep(0.05)
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
    )

    results: list[str] = []

    async def _fetch():
        results.append(await tp.get_token())

    async with anyio.create_task_group() as tg:
        tg.start_soon(_fetch)
        tg.start_soon(_fetch)

    # The lock serialises the check-then-fetch cycle: only one HTTP request,
    # and both callers observe the same cached token.
    assert calls["n"] == 1
    assert results == ["tok1", "tok1"]


# --- Dimension discovery via gateway GET /v1/models -------------------------


def _mock_async_client(monkeypatch, handler):
    """Route every httpx.AsyncClient through a MockTransport (mirrors the
    token-provider tests above)."""
    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)


async def test_detect_dimension_from_models_endpoint(monkeypatch):
    """_detect_dimension() resolves the dimension from /v1/models with no embed
    call — the regression that crashed external-mode startup."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "mistral/mistral-embed",
                        "object": "model",
                        "dimension": 1024,
                    },
                    {
                        "id": "text-embedding-3-large",
                        "object": "model",
                        "dimension": 3072,
                    },
                ],
            },
        )

    _mock_async_client(monkeypatch, handler)
    provider = GatewayProvider(
        base_url="http://gw:8083/v1", embedding_model="mistral/mistral-embed"
    )
    await provider._detect_dimension()
    assert provider.get_dimension() == 1024
    assert seen["url"].endswith("/v1/models")
    assert seen["auth"] is None  # unauthenticated gateway


async def test_detect_dimension_sends_bearer(monkeypatch):
    """When a token provider is configured, discovery presents the M2M bearer."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200, json={"data": [{"id": "mistral/mistral-embed", "dimension": 1024}]}
        )

    _mock_async_client(monkeypatch, handler)
    tp = GatewayTokenProvider(
        token_url="http://idp.example/token", client_id="c", client_secret="s"
    )
    provider = GatewayProvider(
        base_url="http://gw:8083/v1",
        embedding_model="mistral/mistral-embed",
        token_provider=tp,
    )
    await provider._detect_dimension()
    assert provider.get_dimension() == 1024
    assert captured["auth"] == "Bearer tok"


async def test_detect_dimension_non_fatal_on_http_error(monkeypatch):
    """An old gateway without /v1/models (404) must not crash startup —
    dimension stays unknown so lazy detect-on-first-embed still applies."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    _mock_async_client(monkeypatch, handler)
    provider = GatewayProvider(
        base_url="http://gw:8083/v1", embedding_model="mistral/mistral-embed"
    )
    await provider._detect_dimension()  # must not raise
    with pytest.raises(RuntimeError):
        provider.get_dimension()  # still unknown


async def test_detect_dimension_model_absent(monkeypatch):
    """Gateway reachable but doesn't list our model → no dimension set,
    no raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "other", "dimension": 99}]})

    _mock_async_client(monkeypatch, handler)
    provider = GatewayProvider(
        base_url="http://gw:8083/v1", embedding_model="mistral/mistral-embed"
    )
    await provider._detect_dimension()
    with pytest.raises(RuntimeError):
        provider.get_dimension()


async def test_detect_dimension_skips_when_already_known(monkeypatch):
    """If the dimension is already known, discovery makes no HTTP call."""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"data": []})

    _mock_async_client(monkeypatch, handler)
    provider = GatewayProvider(
        base_url="http://gw:8083/v1", embedding_model="mistral/mistral-embed"
    )
    provider._dimension = 1024  # pre-set (e.g. explicit override / OpenAI model)
    await provider._detect_dimension()
    assert called["n"] == 0
    assert provider.get_dimension() == 1024


# --- /v1 base-path normalization --------------------------------------------
# EMBEDDING_GATEWAY_URL is configured as a bare origin (scheme://host:port);
# the provider appends the gateway's /v1 base path so both the OpenAI SDK's
# embed posts ({base}/embeddings) and discovery ({base}/models) land under /v1.


def _client_base(provider: GatewayProvider) -> str:
    return str(provider.client.base_url).rstrip("/")


def test_bare_base_url_gets_v1_base_path():
    provider = GatewayProvider(
        base_url="http://gw:8083", embedding_model="mistral/mistral-embed"
    )
    assert _client_base(provider).endswith("/v1")


def test_v1_base_url_is_idempotent():
    # A URL that already carries /v1 (e.g. legacy config) is not doubled.
    provider = GatewayProvider(
        base_url="http://gw:8083/v1", embedding_model="mistral/mistral-embed"
    )
    base = _client_base(provider)
    assert base.endswith("/v1")
    assert not base.endswith("/v1/v1")


def test_trailing_slash_base_url_normalized():
    provider = GatewayProvider(
        base_url="http://gw:8083/", embedding_model="mistral/mistral-embed"
    )
    base = _client_base(provider)
    assert base.endswith("/v1")
    assert not base.endswith("/v1/v1")


async def test_detect_dimension_with_bare_base_url_hits_v1_models(monkeypatch):
    """End-to-end of the fix: a bare-origin base_url still resolves the
    dimension because discovery lands on /v1/models."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200, json={"data": [{"id": "mistral/mistral-embed", "dimension": 1024}]}
        )

    _mock_async_client(monkeypatch, handler)
    provider = GatewayProvider(
        base_url="http://gw:8083", embedding_model="mistral/mistral-embed"
    )
    await provider._detect_dimension()
    assert provider.get_dimension() == 1024
    assert seen["url"].endswith("/v1/models")
