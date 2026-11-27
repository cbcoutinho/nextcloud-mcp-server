"""Unit tests for ActivityClient: the cursor state it reads off each response.

The header semantics are those of the activity app bundled with Nextcloud 33-35
(``Data::setOffsetFromSince``), which also hold for its 9.x development line.
"""

import string
from dataclasses import asdict

import httpx2
import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from nextcloud_client.activity import ActivityClient, _normalise_objects
from nextcloud_client.http import nextcloud_httpx_client

from tests.strategies import header_text, json_values

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


FILTER_CHARS = string.ascii_letters + string.digits + "_-"
_filter_ids = st.text(st.sampled_from(FILTER_CHARS), max_size=10)


@given(
    st.tuples(
        _filter_ids,
        # URL-meaningful characters first: a uniform draw almost never picks "."
        st.sampled_from("./\\%?#&= ")
        | st.characters(codec="utf-8", exclude_characters=FILTER_CHARS),
        _filter_ids,
    ).map("".join)
)
@example("")
@example("..")  # API_BASE/.. would address the parent route
@example("../../cloud/users")
async def test_filter_id_cannot_leave_the_activity_route(bad):
    """The filter is a path segment: an empty id, or one character outside the
    slug alphabet, is refused before any request is made."""
    with pytest.raises(ValueError, match="Invalid activity filter"):
        await _call(httpx2.Response(200), filter=bad)


@given(_filter_ids.filter(bool))
async def test_any_slug_is_a_valid_filter(filter_id):
    _, request = await _call(_ocs([]), filter=filter_id)
    assert request.url.path == f"{BASE}/{filter_id}"


# ── Response parsing ────────────────────────────────────────────────────


@st.composite
def int_headers(draw):
    """``(header value or None, what the page should report)``.

    Positive ids are cursors; ``0`` and negatives mean "none" (the 9.x line
    sends ``0``); anything non-numeric is ignored rather than raised on.
    """
    kind = draw(st.sampled_from(["absent", "positive", "non_positive", "garbage"]))
    if kind == "absent":
        return None, None
    if kind == "positive":
        n = draw(st.integers(min_value=1))
        return str(n), n
    if kind == "non_positive":
        return str(draw(st.integers(max_value=0))), None
    garbage = draw(header_text.filter(lambda t: not any(c.isdigit() for c in t)))
    return garbage, None


_object_values = st.text().map(lambda t: (t, t)) | st.integers().map(
    lambda n: (n, str(n))
)


@st.composite
def activity_rows(draw):
    """``(row as sent, row as the page should carry it)``.

    ``objects`` arrives as a JSON object, or as ``[]`` when PHP encodes an
    empty map; values may be numbers, which the page carries as strings.
    """
    row = {"activity_id": draw(st.integers(min_value=1)), "subject": draw(st.text())}
    expected = dict(row)
    shape = draw(st.sampled_from(["map", "php_empty", "absent"]))
    if shape == "map":
        pairs = draw(st.dictionaries(st.text(), _object_values, max_size=3))
        row["objects"] = {k: sent for k, (sent, _) in pairs.items()}
        expected["objects"] = {k: want for k, (_, want) in pairs.items()}
    else:
        if shape == "php_empty":
            row["objects"] = []
        expected["objects"] = {}
    return row, expected


@st.composite
def activity_pages(draw):
    """A well-formed response, the ``since`` it answers, and the page it reads as."""
    status = draw(st.sampled_from([200, 204, 304]))
    since = draw(st.integers(min_value=0, max_value=10**6))
    last_header, last_given = draw(int_headers())
    first_header, first_known = draw(int_headers())
    has_more = draw(st.booleans())
    etag = draw(st.none() | header_text)
    headers = {}
    if last_header is not None:
        headers["X-Activity-Last-Given"] = last_header
    if first_header is not None:
        headers["X-Activity-First-Known"] = first_header
    if has_more:
        headers["Link"] = NEXT
    elif draw(st.booleans()):
        headers["Link"] = '<https://nc.example.com/x>; rel="prev"'
    if etag is not None:
        headers["ETag"] = etag
    rows = draw(st.lists(activity_rows(), max_size=3)) if status == 200 else []
    if status == 200:
        response = _ocs([sent for sent, _ in rows], headers=headers)
    else:
        response = httpx2.Response(status, headers=headers)
    expected = {
        "activities": [want for _, want in rows],
        "last_given": last_given,
        "first_known": first_known,
        "has_more": has_more,
        "etag": etag,
        "cursor_reset": since > 0 and first_known is not None,
    }
    return response, since, expected


@given(activity_pages())
async def test_page_carries_exactly_what_the_response_said(case):
    response, since, expected = case
    page, _ = await _call(response, since=since)
    assert asdict(page) == expected


@given(json_values)
@example([])
@example({"12": 3})
def test_objects_of_any_shape_normalise_to_a_string_map(objects):
    """PHP's ``[]`` for an empty map, or anything stranger, never raises."""
    normalised = _normalise_objects(objects)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in normalised.items())
    assert len(normalised) == (len(objects) if isinstance(objects, dict) else 0)
