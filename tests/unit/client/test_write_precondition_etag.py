"""The conditional-write header must tolerate etags mangled in transit.

Reverse proxies rewrite ETags. Apache's ``mod_deflate`` appends ``-gzip`` to
every compressed response (``DeflateAlterETag AddSuffix``, the default), so the
value a client reads back is not the one the origin stored. Passing it through
as ``If-Match`` made Nextcloud reject the write as a concurrent edit, which
broke *every* overwrite of an existing file behind such a proxy.
"""

import pytest

from nextcloud_mcp_server.client.webdav import (
    _normalise_etag,
    _write_precondition_header,
)

pytestmark = pytest.mark.unit


class TestNormaliseEtag:
    def test_plain_etag_is_untouched(self):
        assert _normalise_etag("abc123") == "abc123"

    @pytest.mark.parametrize("suffix", ["-gzip", "-br", "-deflate"])
    def test_content_coding_suffix_is_dropped(self, suffix):
        assert _normalise_etag(f"abc123{suffix}") == "abc123"

    def test_weak_validator_prefix_is_dropped(self):
        assert _normalise_etag('W/"abc123"') == "abc123"

    def test_quotes_are_dropped(self):
        assert _normalise_etag('"abc123"') == "abc123"

    def test_weak_gzipped_and_quoted(self):
        assert _normalise_etag('W/"abc123-gzip"') == "abc123"

    def test_only_a_trailing_suffix_counts(self):
        # A hyphenated name that merely contains "gzip" must survive.
        assert _normalise_etag("gzip-abc123") == "gzip-abc123"

    def test_empty_stays_empty(self):
        assert _normalise_etag("") == ""


class TestWritePreconditionHeader:
    def test_create_only_when_no_etag(self):
        assert _write_precondition_header(None) == {"If-None-Match": "*"}

    def test_force_overwrite(self):
        assert _write_precondition_header("*") == {"If-Match": "*"}

    def test_etag_is_quoted(self):
        assert _write_precondition_header("abc123") == {"If-Match": '"abc123"'}

    def test_mangled_etag_is_repaired_before_use(self):
        assert _write_precondition_header("abc123-gzip") == {"If-Match": '"abc123"'}
