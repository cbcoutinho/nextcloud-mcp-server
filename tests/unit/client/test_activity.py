"""Unit tests for ActivityClient: the cursor state it reads off each response.

The header semantics are those of the activity app bundled with Nextcloud 33-35
(``Data::setOffsetFromSince``), which also hold for its 9.x development line.
"""

import httpx2
import pytest
from nextcloud_client.activity import ActivityClient
from nextcloud_client.http import nextcloud_httpx_client

pytestmark = pytest.mark.unit

BASE = "/ocs/v2.php/apps/activity/api/v2/activity"
NEXT = '<https://nc.example.com/ocs/v2.php/apps/activity/api/v2/activity/all?since=7>; rel="next"'


def _ocs(data, status=200, headers=None) -> httpx2.Response:
    return httpx2.Response(
        status,
        json={"ocs": {"meta": {"statuscode": 200}, "data": data}},
        headers=headers,
    )


async def _call(response: httpx2.Response, **kwargs):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return response

    async with nextcloud_httpx_client(
        base_url="https://nc.example.com", transport=httpx2.MockTransport(handler)
    ) as http:
        page = await ActivityClient(http, "alice").list_activities(**kwargs)
    return page, seen[0]


async def test_page_with_more_to_come():
    activity = {"activity_id": 9, "objects": {"12": "/a.md", "13": "/b.md"}}
    page, request = await _call(
        _ocs([activity], headers={"X-Activity-Last-Given": "7", "Link": NEXT}),
        since=3,
        sort="asc",
    )
    assert request.url.path == f"{BASE}/all"
    assert request.url.params["since"] == "3"
    assert request.url.params["sort"] == "asc"
    assert page.activities == [activity]
    assert page.last_given == 7
    assert page.has_more is True
    assert page.cursor_reset is False


async def test_last_page_has_no_link():
    """A short page is not the end; the missing Link header is."""
    page, _ = await _call(
        _ocs([{"activity_id": 9}], headers={"X-Activity-Last-Given": "9"})
    )
    assert page.has_more is False


async def test_unknown_cursor_is_reported_as_reset():
    """NC33-35 answer 200 from the start of the stream, with First-Known."""
    page, _ = await _call(
        _ocs([{"activity_id": 1}], headers={"X-Activity-First-Known": "1"}), since=999
    )
    assert page.cursor_reset is True
    assert page.first_known == 1


async def test_first_known_without_a_cursor_is_not_a_reset():
    page, _ = await _call(
        _ocs([{"activity_id": 1}], headers={"X-Activity-First-Known": "1"})
    )
    assert page.cursor_reset is False


async def test_not_modified_is_an_empty_page():
    """304 (nothing new) raises in _make_request; the client turns it into a page."""
    page, _ = await _call(httpx2.Response(304, headers={"ETag": '"abc"'}), since=5)
    assert page.activities == []
    assert page.last_given is None
    assert page.etag == '"abc"'
    assert page.cursor_reset is False


async def test_no_content_is_an_empty_page():
    page, _ = await _call(httpx2.Response(204))
    assert page.activities == []


async def test_php_empty_objects_array_becomes_a_dict():
    page, _ = await _call(_ocs([{"activity_id": 1, "objects": []}]))
    assert page.activities[0]["objects"] == {}


async def test_zero_last_given_means_none():
    """The 9.x line sends 0 when the page had no rows."""
    page, _ = await _call(_ocs([], headers={"X-Activity-Last-Given": "0"}))
    assert page.last_given is None


async def test_object_filter_uses_the_filter_route():
    _, request = await _call(
        _ocs([]), filter="files", object_type="files", object_id=42
    )
    assert request.url.path == f"{BASE}/filter"
    assert request.url.params["object_type"] == "files"
    assert request.url.params["object_id"] == "42"


async def test_unknown_filter_raises():
    with pytest.raises(httpx2.HTTPStatusError):
        await _call(httpx2.Response(404), filter="nope")


async def test_list_filters():
    filters = [{"id": "all", "name": "All activities", "icon": "", "priority": 0}]

    async with nextcloud_httpx_client(
        base_url="https://nc.example.com",
        transport=httpx2.MockTransport(lambda r: _ocs(filters)),
    ) as http:
        assert await ActivityClient(http, "alice").list_filters() == filters


@pytest.mark.parametrize("bad", ["../../cloud/users", "all/x", "", "a b"])
async def test_filter_id_cannot_leave_the_activity_route(bad):
    """The filter is a path segment: reject anything that could walk elsewhere."""
    with pytest.raises(ValueError, match="Invalid activity filter"):
        await _call(httpx2.Response(200), filter=bad)
