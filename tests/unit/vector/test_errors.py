"""Unit tests for vector-sync error formatting (card 309)."""

import httpx
import pytest

from nextcloud_mcp_server.vector._errors import format_exception_group


@pytest.mark.unit
def test_format_plain_exception_returns_repr():
    exc = httpx.ConnectError("Connection error")
    assert format_exception_group(exc) == repr(exc)


@pytest.mark.unit
def test_format_exception_group_names_leaf_cause():
    """A single-child group must surface the real ConnectError, not the group's
    useless 'unhandled errors in a TaskGroup' default message."""
    leaf = httpx.ConnectError("Connection error")
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [leaf])

    formatted = format_exception_group(group)

    assert "ConnectError" in formatted
    # Assert the full leaf repr survives, not just the type name -- guards a
    # future format change that kept the type but dropped the message.
    assert repr(leaf) in formatted
    assert "unhandled errors in a TaskGroup" not in formatted
    assert "1 sub-exception" in formatted


@pytest.mark.unit
def test_format_nested_exception_group_flattens_all_leaves():
    inner = BaseExceptionGroup(
        "inner", [ValueError("bad value"), httpx.ConnectError("conn")]
    )
    outer = BaseExceptionGroup("outer", [inner, RuntimeError("boom")])

    formatted = format_exception_group(outer)

    assert "ValueError" in formatted
    assert "ConnectError" in formatted
    assert "RuntimeError" in formatted
    assert "3 sub-exceptions" in formatted


class TestEndpointContext:
    """GH #1345: an httpx timeout reprs as `ReadTimeout('')` and names nothing.

    An operator reading that line could not tell whether the embedding endpoint,
    Nextcloud or Qdrant had timed out.
    """

    def _request(self, url="https://ollama.example:11434/api/embed"):
        return httpx.Request("POST", url)

    @pytest.mark.unit
    def test_timeout_names_the_endpoint(self):
        exc = httpx.ReadTimeout("", request=self._request())

        formatted = format_exception_group(exc)

        assert "ReadTimeout" in formatted
        assert "POST ollama.example:11434/api/embed" in formatted

    @pytest.mark.unit
    def test_endpoint_survives_the_group_flattening(self):
        # The real #1345 shape: the embed runs in an anyio task group, so the
        # timeout reaches the log wrapped in a BaseExceptionGroup.
        leaf = httpx.ReadTimeout("", request=self._request())
        group = BaseExceptionGroup("unhandled errors in a TaskGroup", [leaf])

        assert "/api/embed" in format_exception_group(group)

    @pytest.mark.unit
    def test_credentials_are_never_logged(self):
        # str(httpx.URL) renders inline userinfo in the clear (only repr
        # obfuscates it), and a query string can carry a token — so the URL is
        # rebuilt from safe components rather than stringified.
        exc = httpx.ReadTimeout(
            "",
            request=self._request("https://user:sekrit@ollama:11434/api/embed?tok=xyz"),
        )

        formatted = format_exception_group(exc)

        assert "sekrit" not in formatted
        assert "user" not in formatted
        assert "xyz" not in formatted
        assert "POST ollama:11434/api/embed" in formatted

    @pytest.mark.unit
    def test_status_error_reports_the_code(self):
        request = self._request()
        exc = httpx.HTTPStatusError(
            "server error",
            request=request,
            response=httpx.Response(503, request=request),
        )

        formatted = format_exception_group(exc)

        assert "HTTP 503" in formatted
        assert "/api/embed" in formatted

    @pytest.mark.unit
    def test_request_unset_degrades_to_plain_repr(self):
        # httpx.RequestError.request RAISES RuntimeError when unset rather than
        # returning None, so a naive getattr would propagate out of a logging
        # helper and lose the log line entirely.
        exc = httpx.ReadTimeout("")

        assert format_exception_group(exc) == repr(exc)

    @pytest.mark.unit
    def test_non_httpx_exception_is_unchanged(self):
        exc = ValueError("bad value")

        assert format_exception_group(exc) == repr(exc)
