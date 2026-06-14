"""Unit tests verifying that user-influenced and exception-derived strings
are HTML-escaped before they are rendered into ``HTMLResponse`` content.

The handlers under test are decorated with ``@requires("authenticated")``,
so we install an ``AuthenticationMiddleware`` backed by a trivial backend
that always reports the request as authenticated. We then monkeypatch the
internal helpers (``_get_authenticated_client``, ``is_nextcloud_admin``,
``get_preset``) to drive the handler down the specific code path we want
to exercise.
"""

import pytest
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.auth import webhook_routes
from nextcloud_mcp_server.auth.webhook_routes import (
    WebhookSecretNotConfigured,
    disable_webhook_preset,
    enable_webhook_preset,
)

pytestmark = pytest.mark.unit


class _AlwaysAuthBackend(AuthenticationBackend):
    async def authenticate(self, conn):
        return AuthCredentials(["authenticated"]), SimpleUser("testuser")


def _make_app() -> Starlette:
    return Starlette(
        routes=[
            Route(
                "/app/webhooks/enable/{preset_id:path}",
                enable_webhook_preset,
                methods=["POST"],
            ),
            Route(
                "/app/webhooks/disable/{preset_id:path}",
                disable_webhook_preset,
                methods=["DELETE"],
            ),
        ],
        middleware=[Middleware(AuthenticationMiddleware, backend=_AlwaysAuthBackend())],
    )


def _stub_admin_path(monkeypatch):
    """Make the handler progress past auth/admin checks without real I/O."""

    async def _fake_client(_request):
        return object()  # never actually used because get_preset returns None

    async def _fake_is_admin(_request, _client):
        return True

    monkeypatch.setattr(webhook_routes, "_get_authenticated_client", _fake_client)
    monkeypatch.setattr(webhook_routes, "is_nextcloud_admin", _fake_is_admin)


def test_enable_unknown_preset_id_is_html_escaped(monkeypatch):
    """A `<script>` tag in the preset_id path param must be rendered as
    escaped text, not active markup."""
    _stub_admin_path(monkeypatch)
    monkeypatch.setattr(webhook_routes, "get_preset", lambda _id: None)

    app = _make_app()
    payload = "<script>alert(1)</script>"

    with TestClient(app) as client:
        response = client.post(f"/app/webhooks/enable/{payload}")

    assert response.status_code == 404
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_disable_unknown_preset_id_is_html_escaped(monkeypatch):
    _stub_admin_path(monkeypatch)
    monkeypatch.setattr(webhook_routes, "get_preset", lambda _id: None)

    app = _make_app()
    payload = "<script>alert(2)</script>"

    with TestClient(app) as client:
        response = client.delete(f"/app/webhooks/disable/{payload}")

    assert response.status_code == 404
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in response.text
    assert "<script>alert(2)</script>" not in response.text


def test_enable_exception_message_is_html_escaped(monkeypatch):
    """If the handler raises, the exception text must be escaped before
    it lands in the 500 response body."""

    async def _boom(_request):
        raise RuntimeError("</p><script>x</script>")

    monkeypatch.setattr(webhook_routes, "_get_authenticated_client", _boom)

    app = _make_app()

    with TestClient(app) as client:
        response = client.post("/app/webhooks/enable/notes_sync")

    assert response.status_code == 500
    assert "&lt;/p&gt;&lt;script&gt;x&lt;/script&gt;" in response.text
    assert "<script>x</script>" not in response.text


def test_disable_exception_message_is_html_escaped(monkeypatch):
    async def _boom(_request):
        raise RuntimeError("</p><script>y</script>")

    monkeypatch.setattr(webhook_routes, "_get_authenticated_client", _boom)

    app = _make_app()

    with TestClient(app) as client:
        response = client.delete("/app/webhooks/disable/notes_sync")

    assert response.status_code == 500
    assert "&lt;/p&gt;&lt;script&gt;y&lt;/script&gt;" in response.text
    assert "<script>y</script>" not in response.text


def test_enable_preset_returns_503_when_secret_unset(monkeypatch):
    """Security (GHSA-8vh3-g2qg-2h2c): when registration raises
    WebhookSecretNotConfigured, the handler returns a distinct 503 (not the
    generic 500 exception branch) so the UI can tell operators webhooks are
    disabled rather than broken."""
    _stub_admin_path(monkeypatch)

    def _raise():
        raise WebhookSecretNotConfigured("no secret")

    # _register_preset_webhooks calls webhook_auth_pair() internally; patching
    # the module global routes the call through this raising stub.
    monkeypatch.setattr(webhook_routes, "webhook_auth_pair", _raise)

    app = _make_app()

    with TestClient(app) as client:
        response = client.post("/app/webhooks/enable/notes_sync")

    assert response.status_code == 503
    assert "WEBHOOK_SECRET" in response.text
