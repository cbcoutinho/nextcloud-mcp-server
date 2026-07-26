"""Transport-security and CORS configuration.

Both were hardcoded literals before: ``enable_dns_rebinding_protection=False``
and ``allow_origins=["*"]``. Making them settings must not change any existing
deployment's behaviour, which is what most of these assert.

Overrides go through ``set_override`` — the documented path, which also drops the
settings caches — with ``_reload_config`` restoring the baseline afterwards.
"""

from __future__ import annotations

import logging

import pytest

from nextcloud_mcp_server.app import _build_transport_security, _csv_setting
from nextcloud_mcp_server.config import _reload_config, set_override

pytestmark = pytest.mark.unit


@pytest.fixture
def clean_settings():
    """Override settings, restoring each key's default afterwards.

    ``_reload_config`` only drops the memoisation cache — a ``set_override``
    value persists in dynaconf, so without explicit restoration the overrides
    leak into whichever test runs next (they did, on the first attempt).
    """
    from nextcloud_mcp_server.config import _DEFAULTS

    applied: list[str] = []

    def _apply(key: str, value) -> None:
        applied.append(key)
        set_override(key, value)

    _reload_config()
    yield _apply
    for key in applied:
        set_override(key, _DEFAULTS[key.lower()])
    _reload_config()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", []),
        ("*", ["*"]),
        ("a.example", ["a.example"]),
        ("a.example,b.example", ["a.example", "b.example"]),
        (" a.example , b.example ", ["a.example", "b.example"]),
        ("a.example,,b.example", ["a.example", "b.example"]),
        (",", []),
    ],
)
def test_csv_setting(raw, expected):
    assert _csv_setting(raw) == expected


def test_protection_defaults_off(clean_settings):
    """The default must preserve what was hardcoded: MCP 1.23+'s localhost-only
    host checking breaks k8s/Docker service DNS names."""
    settings = _build_transport_security()

    assert settings.enable_dns_rebinding_protection is False


def test_protection_can_be_enabled_with_allowlists(clean_settings):
    clean_settings("MCP_DNS_REBINDING_PROTECTION", "true")
    clean_settings("MCP_ALLOWED_HOSTS", "mcp.internal,mcp.svc.cluster.local")
    clean_settings("MCP_ALLOWED_ORIGINS", "https://app.example")

    settings = _build_transport_security()

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["mcp.internal", "mcp.svc.cluster.local"]
    assert settings.allowed_origins == ["https://app.example"]


def test_enabled_without_allowed_hosts_refuses_to_start(clean_settings):
    """Enabling the protection with no allowlist would reject *every* request.

    The SDK's _validate_host returns False for any host not in allowed_hosts, and
    the default is []. So the flag alone produces a server that answers nothing —
    an outage that looks nothing like a config error. Fail at startup instead,
    with the remedy in the message.
    """
    clean_settings("MCP_DNS_REBINDING_PROTECTION", "true")

    with pytest.raises(ValueError, match="MCP_ALLOWED_HOSTS"):
        _build_transport_security()


def test_origins_are_optional_when_hosts_are_set(clean_settings):
    """Origin is absent on same-origin requests and _validate_origin allows that,
    so an empty origin list is safe — unlike an empty host list."""
    clean_settings("MCP_DNS_REBINDING_PROTECTION", "true")
    clean_settings("MCP_ALLOWED_HOSTS", "mcp.internal")

    settings = _build_transport_security()

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["mcp.internal"]


def test_allowlist_without_protection_warns(clean_settings, caplog):
    """An allowlist set while the protection is off does nothing — a
    misconfiguration worth surfacing rather than silently ignoring."""
    clean_settings("MCP_ALLOWED_HOSTS", "mcp.internal")

    with caplog.at_level(logging.WARNING, logger="nextcloud_mcp_server.app"):
        settings = _build_transport_security()

    assert settings.enable_dns_rebinding_protection is False
    assert any("have no effect" in r.message for r in caplog.records)
