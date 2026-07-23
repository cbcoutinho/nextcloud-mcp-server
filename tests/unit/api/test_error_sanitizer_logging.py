"""``_sanitize_error_for_client`` must log the traceback.

This helper backs every catch-all 500 in the management/access surface, and the
client deliberately receives only a generic message — so this log line is the
sole record of what actually failed. It previously used ``logger.error(...)``,
which discards the stack, leaving a 500 from a wide try block with no way to
tell (say) a Qdrant outage from a TypeError in response building.
"""

import logging

import pytest

from nextcloud_mcp_server.api.management import _sanitize_error_for_client

pytestmark = pytest.mark.unit


def test_sanitizer_logs_traceback(caplog):
    caplog.set_level(logging.ERROR, logger="nextcloud_mcp_server.api.management")

    try:
        raise RuntimeError("qdrant is down")
    except RuntimeError as e:
        _sanitize_error_for_client(e, "some_context")

    record = next(
        r for r in caplog.records if r.name == "nextcloud_mcp_server.api.management"
    )
    # exc_info is what carries the traceback; logger.error(..., e) leaves it None.
    assert record.exc_info is not None, "traceback was not captured"
    assert record.exc_info[0] is RuntimeError
    assert "some_context" in record.getMessage()


def test_sanitizer_never_leaks_the_error_to_the_client():
    """The traceback goes to the log; the caller still gets the generic message."""
    try:
        raise RuntimeError("secret-host.internal:5432 refused connection")
    except RuntimeError as e:
        msg = _sanitize_error_for_client(e, "some_context")

    assert "secret-host.internal" not in msg
    assert msg == "An internal error occurred. Please contact your administrator."
