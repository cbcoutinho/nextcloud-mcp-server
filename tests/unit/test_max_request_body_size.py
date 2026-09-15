"""The Streamable HTTP body limit, derived from ``WEBDAV_WRITE_MAX_MB``.

mcp 2.x caps POST bodies at 4 MiB and answers 413 *before* parsing the JSON, so
without this derivation a `nc_webdav_write_file` call well under the advertised
50 MB limit would be refused by the transport — with an opaque status code
instead of that tool's explanatory ``ToolError``.

Pure arithmetic gating a user-visible behaviour, which is exactly the kind of
thing that regresses silently: get the base64 factor wrong and large writes
start failing at the transport with nothing to explain why.
"""

from types import SimpleNamespace

import pytest

from nextcloud_mcp_server.app import _max_request_body_size

pytestmark = pytest.mark.unit

MIB = 1024 * 1024
SDK_FLOOR = 4 * MIB


def _patch_max_mb(mocker, value):
    mocker.patch(
        "nextcloud_mcp_server.app.get_settings",
        return_value=SimpleNamespace(webdav_write_max_mb=value),
    )


def test_default_50mb_setting_leaves_room_for_the_base64_wire_form(mocker):
    """base64 inflates by 4/3, and the tool decodes rather than streaming."""
    _patch_max_mb(mocker, 50.0)

    limit = _max_request_body_size()

    # The whole point: a 50 MB file must still fit once base64-encoded.
    assert limit > 50 * MIB * 4 / 3
    assert limit == int(50 * MIB * 4 / 3) + MIB


@pytest.mark.parametrize("max_mb", [0.0, 0, None, 1.0])
def test_never_drops_below_the_sdk_default(mocker, max_mb):
    """A small or unset setting must not tighten the transport below 4 MiB.

    ``0``/``None`` mean "no app-level cap" to the write tool, which must not be
    read here as "no bytes allowed".
    """
    _patch_max_mb(mocker, max_mb)

    assert _max_request_body_size() == SDK_FLOOR


def test_scales_with_the_setting(mocker):
    """Raising WEBDAV_WRITE_MAX_MB must actually raise the transport limit."""
    _patch_max_mb(mocker, 50.0)
    at_50 = _max_request_body_size()
    _patch_max_mb(mocker, 200.0)
    at_200 = _max_request_body_size()

    assert at_200 > at_50
    assert at_200 >= 200 * MIB, "a 200 MB file must at least fit before encoding"
