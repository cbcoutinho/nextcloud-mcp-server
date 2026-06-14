"""Unit tests for ``_get_webhook_uri`` priority order and the
``webhook_auth_pair`` registration helper.

Cloud deployments register the webhook URI returned by this function with
Nextcloud. ECS Fargate also exposes ``/.dockerenv``, so an explicit public
URL must win over the docker auto-detection branch. The URL fields are
read via dynaconf (``Settings``), so tests patch ``get_settings`` directly.
The docker-detection markers (``/.dockerenv``, ``DOCKER_CONTAINER``,
``NEXTCLOUD_MCP_SERVICE_NAME``, ``NEXTCLOUD_MCP_PORT``) remain on
``os.getenv`` and are exercised via env-var monkeypatching.
"""

import pytest

from nextcloud_mcp_server.auth import webhook_routes
from nextcloud_mcp_server.auth.webhook_routes import (
    WebhookSecretNotConfigured,
    _get_webhook_uri,
    webhook_auth_pair,
)
from nextcloud_mcp_server.config import Settings

DOCKER_ENV_VARS = (
    "NEXTCLOUD_MCP_SERVICE_NAME",
    "NEXTCLOUD_MCP_PORT",
    "DOCKER_CONTAINER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in DOCKER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _patch_settings(monkeypatch, **overrides) -> None:
    """Make ``get_settings()`` (as called inside webhook_routes) return a
    Settings instance with the given URL/secret fields set; everything else
    falls back to the dataclass defaults."""
    monkeypatch.setattr(
        webhook_routes,
        "get_settings",
        lambda: Settings(**overrides),
    )


def _no_docker_markers(monkeypatch):
    monkeypatch.setattr(
        "nextcloud_mcp_server.auth.webhook_routes.os.path.exists",
        lambda _path: False,
    )


def _docker_markers(monkeypatch):
    monkeypatch.setattr(
        "nextcloud_mcp_server.auth.webhook_routes.os.path.exists",
        lambda path: path == "/.dockerenv",
    )


@pytest.mark.unit
def test_webhook_internal_url_wins_over_everything(monkeypatch):
    _patch_settings(
        monkeypatch,
        webhook_internal_url="https://internal.example.com",
        nextcloud_mcp_server_url="https://public.example.com",
    )
    _docker_markers(monkeypatch)

    assert _get_webhook_uri() == "https://internal.example.com/webhooks/nextcloud"


@pytest.mark.unit
def test_public_url_wins_over_docker_detection(monkeypatch):
    """The bug-fix case: ECS containers have /.dockerenv but a public URL is
    set. Docker auto-detection must NOT clobber the explicit public URL."""
    _patch_settings(
        monkeypatch,
        nextcloud_mcp_server_url="https://holy-bluegill.astrolabecloud.com",
    )
    _docker_markers(monkeypatch)

    assert (
        _get_webhook_uri()
        == "https://holy-bluegill.astrolabecloud.com/webhooks/nextcloud"
    )


@pytest.mark.unit
def test_docker_detection_used_when_no_public_url(monkeypatch):
    """docker-compose dev: no public URL set, /.dockerenv exists → use the
    docker-compose service name."""
    _patch_settings(monkeypatch)
    _docker_markers(monkeypatch)

    assert _get_webhook_uri() == "http://mcp:8000/webhooks/nextcloud"


@pytest.mark.unit
def test_docker_detection_honors_service_name_and_port_overrides(monkeypatch):
    _patch_settings(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_MCP_SERVICE_NAME", "mcp-login-flow")
    monkeypatch.setenv("NEXTCLOUD_MCP_PORT", "8004")
    _docker_markers(monkeypatch)

    assert _get_webhook_uri() == "http://mcp-login-flow:8004/webhooks/nextcloud"


@pytest.mark.unit
def test_docker_container_env_var_triggers_docker_branch(monkeypatch):
    _patch_settings(monkeypatch)
    monkeypatch.setenv("DOCKER_CONTAINER", "true")
    _no_docker_markers(monkeypatch)

    assert _get_webhook_uri() == "http://mcp:8000/webhooks/nextcloud"


@pytest.mark.unit
def test_localhost_fallback_when_nothing_set(monkeypatch):
    _patch_settings(monkeypatch)
    _no_docker_markers(monkeypatch)

    assert _get_webhook_uri() == "http://localhost:8000/webhooks/nextcloud"


# --- webhook_auth_pair() --------------------------------------------------


@pytest.mark.unit
def test_auth_pair_raises_when_secret_unset(monkeypatch):
    """Security (GHSA-8vh3-g2qg-2h2c): no secret => no webhook registration.
    The helper raises instead of returning an ``authMethod="none"`` pair."""
    _patch_settings(monkeypatch, webhook_secret=None)
    with pytest.raises(WebhookSecretNotConfigured):
        webhook_auth_pair()


@pytest.mark.unit
def test_auth_pair_emits_bearer_header_when_secret_set(monkeypatch):
    _patch_settings(monkeypatch, webhook_secret="supersecret")
    assert webhook_auth_pair() == (
        "header",
        {"Authorization": "Bearer supersecret"},
    )
