"""Unit tests for paged WebDAV SEARCH (complete folder discovery).

These cover ``WebDAVClient.search_files_all`` -- the helper the vector-sync
scanner uses to expand a tagged folder into *all* its descendants, rather than
just Nextcloud's default ~100-result SEARCH page (which silently truncated large
folders and left documents unindexed).

The behaviour we pin:
  * offset paging when the server honours ``<d:firstresult>``;
  * automatic fallback to a single bounded fetch when the server *ignores*
    offset (the real Nextcloud 31 behaviour -- a page repeats already-seen rows);
  * a single short page terminates immediately;
  * crossing ``max_results`` warns + increments the truncation metric;
  * ``_build_search_xml`` emits the offset element only when asked.
"""

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from nextcloud_mcp_server.client.webdav import WebDAVClient

pytestmark = pytest.mark.unit


def _make_client(mocker) -> Any:
    # Returned as Any so tests can reassign mocked search methods without
    # tripping ty's invalid-assignment on the real WebDAVClient signatures.
    client: Any = WebDAVClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")
    return client


def _corpus(n: int) -> list[dict]:
    return [{"file_id": i, "path": f"/dir/f{i}.pdf"} for i in range(n)]


async def test_single_short_page_returns_all_in_one_call(mocker):
    """A folder smaller than the page size resolves in a single SEARCH."""
    client = _make_client(mocker)
    client.search_files = AsyncMock(return_value=_corpus(10))

    results = await client.search_files_all(scope="dir", page_size=500)

    assert [r["file_id"] for r in results] == list(range(10))
    client.search_files.assert_awaited_once()


async def test_offset_honored_pages_through_entire_corpus(mocker):
    """When the server honours offset, every page is fetched until exhausted."""
    client = _make_client(mocker)
    corpus = _corpus(250)

    def fake(*, limit, offset=0, **_):
        return corpus[offset : offset + limit]

    client.search_files = AsyncMock(side_effect=fake)

    results = await client.search_files_all(scope="dir", page_size=100)

    assert [r["file_id"] for r in results] == list(range(250))
    # 100, 100, 50 -> three pages, no fallback needed
    assert client.search_files.await_count == 3


async def test_offset_ignored_falls_back_to_single_fetch(mocker):
    """Real Nextcloud ignores offset; we must still return the full corpus."""
    client = _make_client(mocker)
    corpus = _corpus(250)

    def fake(*, limit, offset=0, **_):
        # offset IGNORED: always return the first ``limit`` rows.
        return corpus[:limit]

    client.search_files = AsyncMock(side_effect=fake)

    results = await client.search_files_all(scope="dir", page_size=100)

    # page0 (0..99) -> page1(offset=100) repeats 0..99 -> detected ->
    # single fetch with the large ceiling returns everything.
    assert [r["file_id"] for r in results] == list(range(250))


async def test_truncation_warns_and_increments_metric(mocker):
    """Hitting max_results must surface (warn + metric), never silently drop."""
    client = _make_client(mocker)
    corpus = _corpus(20)

    def fake(*, limit, offset=0, **_):
        return corpus[offset : offset + limit]

    client.search_files = AsyncMock(side_effect=fake)
    metric = mocker.patch(
        "nextcloud_mcp_server.client.webdav.document_scan_truncated_total"
    )

    results = await client.search_files_all(scope="dir", page_size=5, max_results=5)

    assert len(results) == 5
    metric.inc.assert_called_once()


async def test_offset_ignored_fallback_truncation_metric(mocker):
    """The fallback path also reports truncation when it hits the ceiling."""
    client = _make_client(mocker)
    corpus = _corpus(40)

    def fake(*, limit, offset=0, **_):
        return corpus[:limit]  # offset ignored

    client.search_files = AsyncMock(side_effect=fake)
    metric = mocker.patch(
        "nextcloud_mcp_server.client.webdav.document_scan_truncated_total"
    )

    results = await client.search_files_all(scope="dir", page_size=10, max_results=10)

    assert len(results) == 10
    metric.inc.assert_called_once()


async def test_offset_page_exception_falls_back_to_single_fetch(mocker):
    """If an offset page *raises* (e.g. server rejects firstresult), the
    exception fallback must still return the full corpus -- and it must be
    awaited (regression guard for a missing ``await``)."""
    client = _make_client(mocker)
    corpus = _corpus(80)

    def fake(*, limit, offset=0, **_):
        if offset > 0:
            raise RuntimeError("server rejected <d:firstresult>")
        return corpus[:limit]

    client.search_files = AsyncMock(side_effect=fake)

    results = await client.search_files_all(scope="dir", page_size=50)

    # page0 (0..49) fills; page1(offset=50) raises -> fallback single fetch
    # returns the whole corpus. A non-awaited coroutine would fail these.
    assert isinstance(results, list)
    assert [r["file_id"] for r in results] == list(range(80))


async def test_offset_page_exception_at_offset_zero_propagates(mocker):
    """A failure on the very first page is a real error, not a paging quirk."""
    client = _make_client(mocker)
    client.search_files = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await client.search_files_all(scope="dir", page_size=50)


async def test_find_all_by_type_delegates_to_search_files_all(mocker):
    client = _make_client(mocker)
    client.search_files_all = AsyncMock(return_value=_corpus(3))

    results = await client.find_all_by_type("application/pdf", scope="dir")

    assert len(results) == 3
    kwargs = client.search_files_all.await_args.kwargs
    assert kwargs["scope"] == "dir"
    assert "fileid" in kwargs["properties"]
    assert "application/pdf" in kwargs["where_conditions"]


def test_type_search_args_escapes_mime_type(mocker):
    """A MIME value with XML metacharacters must not break / inject into the SEARCH."""
    client = _make_client(mocker)
    where, properties = client._type_search_args("application/pdf<&>")
    # The injected metacharacters are escaped inside the <d:literal>, so they
    # can't break the SEARCH XML or introduce new elements.
    assert "pdf&lt;&amp;&gt;" in where
    assert "pdf<&>" not in where
    assert "fileid" in properties


def test_build_search_xml_emits_offset_only_when_set(mocker):
    client = _make_client(mocker)

    paged = client._build_search_xml(
        scope="dir",
        where_conditions="",
        properties=["fileid"],
        order_by=None,
        limit=100,
        offset=200,
    )
    assert "<d:nresults>100</d:nresults>" in paged
    assert "<d:firstresult>200</d:firstresult>" in paged

    unlimited = client._build_search_xml(
        scope="dir",
        where_conditions="",
        properties=["fileid"],
        order_by=None,
        limit=None,
        offset=None,
    )
    assert "<d:limit>" not in unlimited
