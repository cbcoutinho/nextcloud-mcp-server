"""Unit test configuration — shared fixtures for all unit tests."""

import pytest

# Re-export the parametrized storage backend fixture so it's auto-discovered
# by every unit test that names it as a parameter, without each test module
# having to import it explicitly.
from tests.fixtures.storage_backend import storage_backend  # noqa: F401


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
    from nextcloud_mcp_server import config as _config

    _config._dynaconf.reload()
    _config._bg_ops_advisories_logged = False
