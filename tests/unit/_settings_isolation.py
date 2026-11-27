"""Settings isolation for the unit tier: reload dynaconf after a test only when
the test changed config. Used by the autouse fixture in ``conftest.py`` and
tested in ``test_settings_isolation.py``. See docs/testing.md, "Settings
isolation"."""

import copy
import os

from nextcloud_mcp_server import config as _config


def env_without_pytest_bookkeeping() -> dict[str, str]:
    """``os.environ`` minus ``PYTEST_CURRENT_TEST``, which pytest rewrites for
    every phase of every test and so always differs."""
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def settings_baseline() -> dict:
    """A fresh load of the current environment's settings. Deep-copied, so a
    test mutating a nested value in place can't also change the baseline."""
    _config._dynaconf.reload()
    return copy.deepcopy(_config._dynaconf.as_dict())


def restore_settings(env_before: dict[str, str], baseline: dict) -> bool:
    """Reload dynaconf if the environment or the settings drifted from
    ``baseline``. Returns whether it reloaded."""
    if (
        env_without_pytest_bookkeeping() == env_before
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
