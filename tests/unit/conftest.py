"""Unit test configuration — shared fixtures for all unit tests."""

import os
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


def _env_without_pytest_bookkeeping() -> dict[str, str]:
    """``os.environ`` minus ``PYTEST_CURRENT_TEST``, which pytest rewrites for
    every phase of every test and so always differs."""
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def restore_settings(env_before: dict[str, str], baseline: dict) -> bool:
    """Reload dynaconf if the environment or the settings drifted from
    ``baseline``. Returns whether it reloaded. Tested in
    ``test_settings_isolation.py``."""
    from nextcloud_mcp_server import config as _config

    if (
        _env_without_pytest_bookkeeping() == env_before
        and _config._dynaconf.as_dict() == baseline
    ):
        return False
    _config._dynaconf.reload()
    # A key added with set_override() survives reload(). Left in place, it
    # would make every later test differ from the baseline and pay the
    # reload, as TEST_CACHE_INVALIDATION_PROBE once did for ~2,700 tests.
    # Only keys absent from the baseline are removed: re-setting a changed
    # key would pin an override that a later monkeypatch.setenv can't beat.
    for key in _config._dynaconf.as_dict().keys() - baseline.keys():
        _config._dynaconf.unset(key, force=True)
    return True


@pytest.fixture(scope="session")
def _dynaconf_baseline() -> dict:
    """The settings every test should start from: a fresh load of the
    session's environment."""
    from nextcloud_mcp_server import config as _config

    _config._dynaconf.reload()
    return _config._dynaconf.as_dict()


@pytest.fixture(autouse=True)
def _reload_dynaconf_after_test(_dynaconf_baseline: dict):
    """Ensure dynaconf cache is clean between tests.

    Dynaconf caches env var values at load time. Tests that modify os.environ
    must call _reload_config() to refresh the cache. This fixture reloads
    after each test to prevent leaked state.

    A reload costs ~40 ms. Doing it after every one of ~5,000 tests was about
    half of the unit tier's run time, so it now runs only when the test left
    something different: the environment, or the loaded settings (a test that
    reloaded with a patched env, or called ``set_override``). Both checks cost
    well under a millisecond. Autouse fixtures are torn down last, so
    ``monkeypatch`` has already restored ``os.environ`` by this point.

    Uses _dynaconf.reload() directly (without validate_all) since the
    real env may have values that don't pass validators. Tests that need
    validation should call _reload_config() explicitly.
    """
    env_before = _env_without_pytest_bookkeeping()
    yield
    from nextcloud_mcp_server import config as _config

    restore_settings(env_before, _dynaconf_baseline)
    # get_settings()/get_nextcloud_ssl_verify()/get_nextcloud_http_keepalive()
    # are @functools.cache'd; reloading dynaconf alone won't refresh them, so
    # clear the settings caches too or a cached value leaks into later tests.
    _config._clear_settings_caches()
    _config._bg_ops_advisories_logged = False
