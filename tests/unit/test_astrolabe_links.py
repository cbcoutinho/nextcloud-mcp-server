"""Unit tests for Astrolabe deep-link construction.

The consumer side already exists: Astrolabe's ``src/App.vue`` opens its chunk
viewer from ``doc_type``/``doc_id``/``chunk_start``/``chunk_end`` on the app
root, and ``parseInt``s the numeric ones. These tests pin the producer against
that contract — a link missing one of the four opens nothing at all, so
"degraded but present" is not an option here.
"""

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from nextcloud_mcp_server.astrolabe_links import (
    ASTROLABE_APP_PATH,
    astrolabe_browser_base,
    chunk_url,
)

pytestmark = pytest.mark.unit

BASE = "https://nc.example.com"


def _fake_settings(
    public_url: str | None = None,
    public_issuer_url: str | None = None,
    host: str | None = None,
) -> SimpleNamespace:
    """Settings-shaped object exposing only what the helper reads.

    ``nextcloud_browser_url`` mirrors the real property's fallback chain
    (public_url → public_issuer_url → host).
    """
    return SimpleNamespace(
        nextcloud_public_url=public_url,
        nextcloud_public_issuer_url=public_issuer_url,
        nextcloud_host=host,
        nextcloud_browser_url=public_url or public_issuer_url or host,
    )


def _params(url: str) -> dict[str, str]:
    """Single-valued query parameters of ``url``."""
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# --- astrolabe_browser_base -------------------------------------------------


def test_base_prefers_public_url_over_issuer_and_host():
    """In external-IdP mode the issuer is the IdP, not Nextcloud."""
    fake = _fake_settings(
        public_url=BASE,
        public_issuer_url="https://keycloak.example.com/realms/x",
        host="http://internal:8080",
    )
    with patch("nextcloud_mcp_server.astrolabe_links.get_settings", return_value=fake):
        assert astrolabe_browser_base() == BASE


def test_base_strips_trailing_slash():
    """Otherwise the joined path would carry a doubled slash."""
    fake = _fake_settings(public_url=f"{BASE}/")
    with patch("nextcloud_mcp_server.astrolabe_links.get_settings", return_value=fake):
        assert astrolabe_browser_base() == BASE


def test_base_is_none_when_nothing_configured():
    with patch(
        "nextcloud_mcp_server.astrolabe_links.get_settings",
        return_value=_fake_settings(),
    ):
        assert astrolabe_browser_base() is None


def test_base_is_none_and_warns_when_scheme_missing(caplog):
    """A bare hostname yields a non-clickable link, so surface it instead."""
    fake = _fake_settings(host="internal-host:8080")
    with patch("nextcloud_mcp_server.astrolabe_links.get_settings", return_value=fake):
        with caplog.at_level("WARNING", logger="nextcloud_mcp_server.astrolabe_links"):
            result = astrolabe_browser_base()
    assert result is None
    assert any("missing an http:// or https://" in r.message for r in caplog.records), (
        f"expected scheme-missing warning, got {[r.message for r in caplog.records]}"
    )


def test_base_allows_plain_http():
    """Local and self-hosted instances are routinely served over http (the dev
    compose stack is http://localhost:8080). Demanding TLS would strip the link
    from exactly the deployments that need it."""
    fake = _fake_settings(host="http://localhost:8080")
    with patch("nextcloud_mcp_server.astrolabe_links.get_settings", return_value=fake):
        assert astrolabe_browser_base() == "http://localhost:8080"


@pytest.mark.parametrize(
    "configured",
    [
        "https:",  # scheme, no host — a startswith check would let this through
        "ftp://nc.example.com",  # a browser will not open this as a page
        "//nc.example.com",  # protocol-relative, meaningless outside a document
    ],
)
def test_base_is_none_for_unopenable_urls(configured):
    fake = _fake_settings(host=configured)
    with patch("nextcloud_mcp_server.astrolabe_links.get_settings", return_value=fake):
        assert astrolabe_browser_base() is None


def test_base_accepts_uppercase_scheme():
    """urlparse normalizes the scheme, so HTTPS:// is a working URL rather than
    a misconfiguration — a prefix match would have rejected it."""
    fake = _fake_settings(host="HTTPS://nc.example.com")
    with patch("nextcloud_mcp_server.astrolabe_links.get_settings", return_value=fake):
        assert astrolabe_browser_base() == "HTTPS://nc.example.com"


# --- chunk_url --------------------------------------------------------------


def test_chunk_url_full_shape():
    url = chunk_url(
        BASE,
        doc_type="file",
        doc_id=384194,
        chunk_start=37636,
        chunk_end=38594,
        title="report.pdf",
        path="/Docs/report.pdf",
        page_number=22,
        chunk_index=48,
        total_chunks=100,
    )
    assert url is not None
    assert url.startswith(f"{BASE}{ASTROLABE_APP_PATH}?")
    assert _params(url) == {
        "doc_type": "file",
        "doc_id": "384194",
        "chunk_start": "37636",
        "chunk_end": "38594",
        "title": "report.pdf",
        "path": "/Docs/report.pdf",
        "page_number": "22",
        "chunk_index": "48",
        "total_chunks": "100",
    }


def test_chunk_url_escapes_spaces_and_slashes():
    """Real corpora have spaces in filenames; an unescaped one truncates the
    query string at the space when the link is pasted or auto-linked."""
    url = chunk_url(
        BASE,
        doc_type="file",
        doc_id=7,
        chunk_start=0,
        chunk_end=10,
        title="Q3 report & notes.pdf",
        path="/My Docs/Q3 report & notes.pdf",
    )
    assert url is not None
    assert " " not in url
    # Escaped on the wire, but round-trips to the original values.
    assert _params(url)["path"] == "/My Docs/Q3 report & notes.pdf"
    assert _params(url)["title"] == "Q3 report & notes.pdf"


def test_chunk_url_omits_unset_optionals():
    """`page_number=None` must not become the literal string 'None', which
    Astrolabe would parseInt into NaN."""
    url = chunk_url(BASE, doc_type="note", doc_id=1, chunk_start=0, chunk_end=5)
    assert url is not None
    assert set(_params(url)) == {"doc_type", "doc_id", "chunk_start", "chunk_end"}


def test_chunk_url_forwards_extra_access_ids():
    """board_id lets a stale link get the same local access re-check as a live
    search result."""
    url = chunk_url(
        BASE,
        doc_type="deck_card",
        doc_id=42,
        chunk_start=0,
        chunk_end=5,
        extra={"board_id": "12"},
    )
    assert url is not None
    assert _params(url)["board_id"] == "12"


def test_chunk_url_named_params_win_a_collision_with_extra():
    """`extra` is an opaque caller-supplied dict; it must not be able to
    redefine a named, documented parameter."""
    url = chunk_url(
        BASE,
        doc_type="file",
        doc_id=1,
        chunk_start=0,
        chunk_end=5,
        title="real title",
        extra={"title": "hijacked", "board_id": "12"},
    )
    assert url is not None
    params = _params(url)
    assert params["title"] == "real title"
    assert params["board_id"] == "12"


def test_chunk_url_is_none_without_a_base():
    assert (
        chunk_url(None, doc_type="file", doc_id=1, chunk_start=0, chunk_end=5) is None
    )


@pytest.mark.parametrize(
    ("chunk_start", "chunk_end"),
    [(None, 10), (0, None), (None, None)],
)
def test_chunk_url_is_none_without_both_offsets(chunk_start, chunk_end):
    """Astrolabe requires all four identifying params together and opens
    nothing if any is absent — a partial link is a dead end, not a degraded
    one."""
    assert (
        chunk_url(
            BASE,
            doc_type="file",
            doc_id=1,
            chunk_start=chunk_start,
            chunk_end=chunk_end,
        )
        is None
    )


def test_chunk_url_keeps_offset_zero():
    """0 is falsy but a perfectly valid first-chunk offset."""
    url = chunk_url(BASE, doc_type="file", doc_id=1, chunk_start=0, chunk_end=0)
    assert url is not None
    assert _params(url)["chunk_start"] == "0"
    assert _params(url)["chunk_end"] == "0"
