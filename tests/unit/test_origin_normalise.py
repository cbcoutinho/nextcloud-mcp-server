"""Tests for ``_normalise_origin`` port + scheme + host normalisation.

The CSRF guard on POST /oauth/logout (PR #758 round-3 review hardening)
compares ``Origin`` / ``Referer`` against the configured ``mcp_server_url``
via ``_normalise_origin``. RFC 6454 §6.2 says browsers omit default ports
(80 for http, 443 for https) from Origin headers, so the function strips
those before comparison.
"""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.auth.browser_oauth_routes import _normalise_origin

pytestmark = pytest.mark.unit

_DEFAULT_PORT = {"http": 80, "https": 443}
_hosts = st.from_regex(r"\A[a-z][a-z0-9-]{0,10}(\.[a-z]{2,5}){1,2}\Z")
_schemes = st.sampled_from(["http", "https"])
_ports = st.integers(min_value=1, max_value=65535)


def _origin(scheme: str, host: str, port: int | None) -> str:
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


@given(_schemes, _hosts)
@example("https", "example.com")
@example("http", "example.com")
def test_explicit_default_port_equals_no_port(scheme, host):
    assert _normalise_origin(
        _origin(scheme, host, _DEFAULT_PORT[scheme])
    ) == _normalise_origin(_origin(scheme, host, None))


@given(_schemes, _hosts, st.sampled_from([str.upper, str.title, str.swapcase]))
def test_host_is_case_insensitive(scheme, host, recase):
    assert _normalise_origin(_origin(scheme, recase(host), None)) == _normalise_origin(
        _origin(scheme, host, None)
    )


@given(_schemes, _schemes, _hosts, _hosts, st.none() | _ports, st.none() | _ports)
@example("https", "http", "example.com", "example.com", None, None)
@example("https", "http", "example.com", "example.com", 443, 80)
@example("https", "https", "example.com", "example.com", 8443, 443)
@example("https", "https", "example.com", "other.com", None, None)
def test_equal_iff_same_scheme_host_and_effective_port(s1, s2, h1, h2, p1, p2):
    """Cross-scheme defaults never collapse; a non-default port never matches
    the bare origin."""

    def effective(scheme, port):
        return _DEFAULT_PORT[scheme] if port is None else port

    same = (s1, h1, effective(s1, p1)) == (s2, h2, effective(s2, p2))
    assert (
        _normalise_origin(_origin(s1, h1, p1)) == _normalise_origin(_origin(s2, h2, p2))
    ) is same
