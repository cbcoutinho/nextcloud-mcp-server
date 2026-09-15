"""The conditional-write header must tolerate etags mangled in transit.

Reverse proxies rewrite ETags. Apache's ``mod_deflate`` appends ``-gzip`` to
every compressed response (``DeflateAlterETag AddSuffix``, the default), so the
value a client reads back is not the one the origin stored. Passing it through
as ``If-Match`` made Nextcloud reject the write as a concurrent edit, which
broke *every* overwrite of an existing file behind such a proxy.

The normalisation itself lives in ``_normalize_etag`` and is covered in
``test_webdav.py``; what is pinned here is that the precondition header runs
values through it rather than re-quoting whatever it was handed.
"""

import pytest

from nextcloud_mcp_server.client.webdav import _write_precondition_header

pytestmark = pytest.mark.unit


class TestWritePreconditionHeader:
    def test_create_only_when_no_etag(self):
        assert _write_precondition_header(None) == {"If-None-Match": "*"}

    def test_force_overwrite(self):
        assert _write_precondition_header("*") == {"If-Match": "*"}

    def test_etag_is_quoted(self):
        assert _write_precondition_header("abc123") == {"If-Match": '"abc123"'}

    def test_mangled_etag_is_repaired_before_use(self):
        assert _write_precondition_header("abc123-gzip") == {"If-Match": '"abc123"'}
