"""Unit test configuration — shared fixtures for all unit tests."""

import pytest


@pytest.fixture(autouse=True)
def _reload_dynaconf_after_test():
    """Ensure dynaconf cache is clean between tests.

    Dynaconf caches env var values at load time. Tests that modify os.environ
    must call _reload_config() to refresh the cache. This fixture reloads
    after each test to prevent leaked state.

    Uses _dynaconf.reload() directly (without validate_all) since the
    real env may have values that don't pass validators. Tests that need
    validation should call _reload_config() explicitly.
    """
    yield
    from nextcloud_mcp_server.config import _dynaconf

    _dynaconf.reload()
