"""Unit test configuration — shared fixtures for all unit tests."""

from pathlib import Path

import pytest

# Re-export the parametrized storage backend fixture so it's auto-discovered
# by every unit test that names it as a parameter, without each test module
# having to import it explicitly.
from tests.fixtures.storage_backend import storage_backend  # noqa: F401

_UNIT_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items):
    """Mark everything under ``tests/unit/`` as ``unit``.

    CI selects this tier by marker (``pytest -m unit``), not by path, so a file
    that forgot ``pytestmark = pytest.mark.unit`` was silently deselected and
    never ran — 263 tests across 9 files were invisible this way, two of them
    failing on master for over a week. Applying the marker by location makes the
    directory the single source of truth, so a new file cannot reopen the hole
    by omission.

    Note this hook fires once with *every* collected item, not just this
    directory's, so it must filter by path.
    """
    for item in items:
        if _UNIT_DIR in item.path.parents:
            item.add_marker(pytest.mark.unit)


@pytest.fixture
def metric_sample():
    """Return a callable reading a Prometheus sample value (0.0 if absent).

    Shared across the metric unit tests so the helper isn't duplicated per
    module.
    """
    from prometheus_client import REGISTRY

    def _sample(name: str, labels: dict[str, str]) -> float:
        return REGISTRY.get_sample_value(name, labels) or 0.0

    return _sample


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
    # get_settings()/get_nextcloud_ssl_verify()/get_nextcloud_http_keepalive()
    # are @functools.cache'd; reloading dynaconf alone won't refresh them, so
    # clear the settings caches too or a cached value leaks into later tests.
    _config._clear_settings_caches()
    _config._bg_ops_advisories_logged = False
