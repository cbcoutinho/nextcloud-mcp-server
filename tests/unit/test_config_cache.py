"""Unit tests for settings / SSL memoization in nextcloud_mcp_server.config.

``get_settings()`` was uncached and re-resolved ~150 dynaconf keys on every
call (5-7x per ingest job — the worker's #2 CPU hotspot). It now memoizes the
resolution and returns a per-call copy, with the cache dropped by
``_reload_config`` (tests) and ``set_override`` (CLI overrides).
"""

from __future__ import annotations

import pytest

import nextcloud_mcp_server.config as config
from nextcloud_mcp_server.config import (
    _reload_config,
    get_nextcloud_http_keepalive,
    get_nextcloud_ssl_verify,
    get_settings,
)


@pytest.mark.unit
def test_build_settings_is_memoized():
    _reload_config()
    assert config._build_settings() is config._build_settings()


@pytest.mark.unit
def test_get_settings_returns_independent_copies():
    _reload_config()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is not s2  # fresh instance per call...
    assert s1 == s2  # ...with identical values


@pytest.mark.unit
def test_get_settings_mutation_does_not_leak():
    """Callers that mutate the returned object (e.g. app.py OAuth flow) stay isolated."""
    _reload_config()
    s1 = get_settings()
    s1.oidc_client_id = "mutated-in-caller"
    s2 = get_settings()
    assert s2.oidc_client_id != "mutated-in-caller"


@pytest.mark.unit
def test_clear_settings_caches_resets_all():
    _reload_config()
    get_settings()
    get_nextcloud_ssl_verify()
    get_nextcloud_http_keepalive()
    assert config._build_settings.cache_info().currsize == 1
    assert get_nextcloud_ssl_verify.cache_info().currsize == 1
    assert get_nextcloud_http_keepalive.cache_info().currsize == 1

    config._clear_settings_caches()

    assert config._build_settings.cache_info().currsize == 0
    assert get_nextcloud_ssl_verify.cache_info().currsize == 0
    assert get_nextcloud_http_keepalive.cache_info().currsize == 0


@pytest.mark.unit
def test_reload_config_invalidates_settings():
    _reload_config()
    base_before = config._build_settings()
    _reload_config()
    base_after = config._build_settings()
    assert base_before is not base_after  # cache dropped -> rebuilt


@pytest.mark.unit
def test_set_override_invalidates_settings(mocker):
    _reload_config()
    get_settings()  # warm the cache
    spy = mocker.spy(config, "_clear_settings_caches")
    # Arbitrary probe key (ignored by the field map) — asserts the invalidation
    # hook fires without perturbing a real setting.
    config.set_override("TEST_CACHE_INVALIDATION_PROBE", "1")
    assert spy.call_count >= 1
    _reload_config()  # reset shared dynaconf/cache state for other tests
