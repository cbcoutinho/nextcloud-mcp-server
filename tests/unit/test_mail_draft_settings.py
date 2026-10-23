"""``MAIL_DRAFT_MARK_AI_GENERATED``: an opt-in operator setting for nc_mail_save_draft."""

import os
from unittest.mock import patch

import pytest
from dynaconf import ValidationError

from nextcloud_mcp_server.config import _reload_config, get_settings


@pytest.fixture(autouse=True)
def restore_config():
    """Each test reloads config under a patched env; reload again afterwards."""
    yield
    _reload_config()


@patch.dict(os.environ, {}, clear=True)
def test_off_by_default():
    _reload_config()
    assert get_settings().mail_draft_mark_ai_generated is False


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
def test_reads_the_env_var(raw, expected):
    with patch.dict(os.environ, {"MAIL_DRAFT_MARK_AI_GENERATED": raw}, clear=True):
        _reload_config()
        assert get_settings().mail_draft_mark_ai_generated is expected


@pytest.mark.parametrize("raw", ["off", "FALSE", "0"])
def test_unparsed_spellings_fail_at_startup(raw):
    """dynaconf leaves these as strings, and a string like "off" is truthy.

    Without the validator they would switch the marker on, the opposite of
    what the operator wrote.
    """
    with patch.dict(os.environ, {"MAIL_DRAFT_MARK_AI_GENERATED": raw}, clear=True):
        with pytest.raises(ValidationError, match="MAIL_DRAFT_MARK_AI_GENERATED"):
            _reload_config()
