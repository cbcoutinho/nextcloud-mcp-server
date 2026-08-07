"""DOCUMENT_OFFICE_TIMEOUT_SECONDS fails fast, like the timeouts it copies.

Without a startup validator, 0 or a negative value reaches
``anyio.fail_after`` in ``_libreoffice.convert`` and expires every conversion
immediately -- so the misconfiguration surfaces as "every .doc/.docx fails to
parse" rather than as a startup error naming the setting.
"""

import os
from unittest.mock import patch

import pytest

from nextcloud_mcp_server.config import _reload_config, get_settings

pytestmark = pytest.mark.unit

SETTING = "DOCUMENT_OFFICE_TIMEOUT_SECONDS"


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_non_positive_timeout_is_rejected_at_startup(bad):
    with patch.dict(os.environ, {SETTING: bad}), pytest.raises(Exception) as excinfo:
        _reload_config()

    assert SETTING.lower() in str(excinfo.value).lower()


def test_a_positive_timeout_is_accepted():
    with patch.dict(os.environ, {SETTING: "45"}):
        _reload_config()
        assert get_settings().document_office_timeout_seconds == 45


def test_the_default_is_used_when_unset():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(SETTING, None)
        _reload_config()
        assert get_settings().document_office_timeout_seconds == 120.0
