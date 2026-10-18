"""``nextcloud_mcp_server.plugin_api``, the import surface for plugins.

That plugins import nothing else is checked on the plugin side (SAR:
``packages/nextcloud-mcp-sar/tests/test_sar_plugin_boundary.py``).
"""

import pytest

from nextcloud_mcp_server import plugin_api

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", plugin_api.__all__)
def test_every_plugin_api_name_resolves(name):
    assert getattr(plugin_api, name) is not None


def test_unknown_names_raise_attribute_error():
    with pytest.raises(AttributeError, match="no_such_name"):
        plugin_api.no_such_name  # noqa: B018
