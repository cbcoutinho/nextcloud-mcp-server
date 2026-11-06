"""Tests for _safe_next_url, the open-redirect guard for ``?next=`` params.

Pins the contract that any target a browser would resolve off our origin falls
back to the default (open redirect flagged on PR #758).
"""

from urllib.parse import urljoin, urlsplit

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.auth.browser_oauth_routes import _safe_next_url

pytestmark = pytest.mark.unit

ORIGIN = "https://mcp.example"
DEFAULT = "/default"


def _browser_resolves(location: str) -> str:
    """Where a browser sends a ``Location`` relative to ORIGIN.

    WHATWG URL parsing treats ``\\`` as ``/`` in http(s) URLs ("special
    schemes", https://url.spec.whatwg.org/#path-state); ``urljoin`` does not,
    so apply that one rule before joining. The guard does not rely on this
    model being complete: it refuses every ``\\`` outright.
    """
    return urljoin(ORIGIN + "/", location.replace("\\", "/"))


# Bias generation towards the prefixes that decide the outcome.
_targets = st.one_of(
    st.none(),
    st.text(),
    st.tuples(
        st.sampled_from(["/", "//", "/\\", "\\", "\\/", "\\\\", "http://", "https:"]),
        st.text(),
    ).map("".join),
)


@given(_targets)
@example("/\\evil.com")  # browsers read it as //evil.com
@example("/\\/evil.com")
@example("\\\\evil.com")
@example("\\foo")  # same-origin for a browser, but no target needs a backslash
@example("//evil.example.com/path")
@example("https://evil.example.com")
@example("/app\nfoo")
@example("/app\x00foo")
@example("/app foo")
@example("relative/path")
def test_never_redirects_off_origin(raw):
    out = _safe_next_url(raw, DEFAULT)

    assert out in (DEFAULT, raw)
    target = urlsplit(_browser_resolves(out))
    assert (target.scheme, target.netloc) == ("https", "mcp.example")
    assert not any(c.isspace() or not c.isprintable() or c == "\\" for c in out)


@given(
    st.from_regex(
        r"\A/[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]*)*(\?[A-Za-z0-9=&]*)?(#[A-Za-z0-9]*)?\Z"
    )
)
@example("/app?x=1&y=2")
@example("/app#frag")
def test_plain_paths_pass_through(path):
    assert _safe_next_url(path, DEFAULT) == path
