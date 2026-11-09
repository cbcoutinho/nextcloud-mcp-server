"""Unit tests for the shared OIDC discovery fetch in token_utils."""

from unittest.mock import patch

import httpx2
import pytest

from nextcloud_mcp_server.auth import token_utils
from nextcloud_mcp_server.auth.token_utils import get_oidc_discovery

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """Reset the in-memory discovery cache between tests."""
    token_utils._discovery_cache.clear()
    yield
    token_utils._discovery_cache.clear()


async def test_discovery_follows_redirect_to_index_php():
    """Discovery fetch must follow 301s.

    Hetzner StorageShare and other Nextcloud installs without pretty URLs
    redirect ``/.well-known/openid-configuration`` to
    ``/index.php/.well-known/openid-configuration``. Without follow_redirects
    the OAuth authorize handler raises HTTPStatusError and returns 500
    (PR #758 round-2 nit 3 consolidated the discovery cache; see
    ``token_utils.get_oidc_discovery``).
    """

    pretty_url = "https://nx.example.com/.well-known/openid-configuration"
    rewritten_url = "https://nx.example.com/index.php/.well-known/openid-configuration"
    discovery_doc = {
        "issuer": "https://nx.example.com",
        "authorization_endpoint": "https://nx.example.com/index.php/apps/oidc/authorize",
        "token_endpoint": "https://nx.example.com/index.php/apps/oidc/token",
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == pretty_url:
            return httpx2.Response(301, headers={"location": rewritten_url})
        if str(request.url) == rewritten_url:
            return httpx2.Response(200, json=discovery_doc)
        return httpx2.Response(404)

    transport = httpx2.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx2.AsyncClient(**kwargs)

    with patch(
        "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
        side_effect=fake_client,
    ) as factory:
        result = await get_oidc_discovery(pretty_url)

    assert result == discovery_doc
    factory.assert_called_once()
    assert factory.call_args.kwargs.get("follow_redirects") is True
