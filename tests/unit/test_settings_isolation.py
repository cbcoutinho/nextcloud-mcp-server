"""The unit tier's settings-isolation teardown (``restore_settings`` in
``tests/unit/conftest.py``). It must reload only when a test changed config,
since a reload costs ~40 ms and the tier has ~5,000 tests. See
docs/testing.md, "Settings isolation"."""

from nextcloud_mcp_server import config
from tests.unit.conftest import _env_without_pytest_bookkeeping, restore_settings


def test_an_untouched_config_is_not_reloaded(_dynaconf_baseline):
    # No mocker.spy on _dynaconf.reload: setting an attribute on dynaconf
    # stores it as a setting, which would itself change as_dict().
    assert not restore_settings(_env_without_pytest_bookkeeping(), _dynaconf_baseline)


def test_a_patched_env_is_reloaded_back_to_the_baseline(
    _dynaconf_baseline, monkeypatch
):
    env_before = _env_without_pytest_bookkeeping()
    monkeypatch.setenv("METRICS_PORT", "9999")
    config._reload_config()
    monkeypatch.undo()  # what pytest does before this fixture's teardown

    assert restore_settings(env_before, _dynaconf_baseline)
    assert config._dynaconf.as_dict() == _dynaconf_baseline


def test_a_leaked_override_key_is_removed(_dynaconf_baseline):
    """A set_override() key survives reload(); left behind, every later test
    would differ from the baseline and pay the reload."""
    config.set_override("SETTINGS_ISOLATION_PROBE", "1")

    assert restore_settings(_env_without_pytest_bookkeeping(), _dynaconf_baseline)
    assert config._dynaconf.as_dict() == _dynaconf_baseline
