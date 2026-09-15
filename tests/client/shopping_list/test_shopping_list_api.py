"""Mocked unit tests for the Shopping List client.

What these pin down is the OCS-specific behaviour the rest of the codebase
cannot see: that the envelope is unwrapped, that the ``OCS-APIRequest`` header
goes out on every call (without it Nextcloud answers 997, not a 4xx), and that
an empty 204 body does not blow up on ``.json()``.
"""

import httpx
import pytest

from nextcloud_mcp_server.client.shopping_list import ShoppingListClient
from tests.client.conftest import create_mock_response

pytestmark = pytest.mark.unit

API = "/ocs/v2.php/apps/shopping_list/api/v1"


def _ocs(data):
    """Wrap *data* in the envelope the app's OCS routes answer with."""
    return {
        "ocs": {
            "meta": {"status": "ok", "statuscode": 200, "message": "OK"},
            "data": data,
        }
    }


def _client(mocker, json_data=None, content=None):
    """A client whose ``_make_request`` is patched, plus the patch itself."""
    mock_response = create_mock_response(
        status_code=200 if content is None else 204,
        json_data=json_data,
        content=content,
    )
    mock_make_request = mocker.patch.object(
        ShoppingListClient, "_make_request", return_value=mock_response
    )
    client = ShoppingListClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    return client, mock_make_request


LIST_FIXTURE = {
    "id": 3,
    "userId": "testuser",
    "title": "Groceries",
    "permission": 1,
    "isOwner": True,
    "isPinned": None,
    "createdAt": "2026-09-11T10:00:00+00:00",
    "updatedAt": "2026-09-11T10:00:00+00:00",
}

ITEM_FIXTURE = {
    "id": 42,
    "listId": 3,
    "name": "flour",
    "quantity": "2",
    "unit": "cups",
    "shopAreaId": 7,
    "checked": False,
    "checkedBy": None,
    "sortOrder": 0,
    "tags": [],
    "createdAt": "2026-09-11T10:00:00+00:00",
    "updatedAt": "2026-09-11T10:00:00+00:00",
}


async def test_get_lists_unwraps_ocs_envelope(mocker):
    client, make_request = _client(mocker, json_data=_ocs([LIST_FIXTURE]))

    lists = await client.get_lists()

    assert lists == [LIST_FIXTURE]
    method, url = make_request.call_args.args
    assert (method, url) == ("GET", f"{API}/lists")


async def test_every_request_sends_the_ocs_header(mocker):
    """Without ``OCS-APIRequest`` Nextcloud answers 997, not a 4xx."""
    client, make_request = _client(mocker, json_data=_ocs(LIST_FIXTURE))

    await client.get_list(3)

    assert make_request.call_args.kwargs["headers"]["OCS-APIRequest"] == "true"


async def test_create_list_posts_title(mocker):
    client, make_request = _client(mocker, json_data=_ocs(LIST_FIXTURE))

    result = await client.create_list("Groceries")

    assert result["title"] == "Groceries"
    assert make_request.call_args.args == ("POST", f"{API}/lists")
    assert make_request.call_args.kwargs["json"] == {"title": "Groceries"}


async def test_delete_list_tolerates_an_empty_204_body(mocker):
    """DELETE answers 204 with no body — ``.json()`` on it would raise."""
    client, make_request = _client(mocker, content=b"")

    assert await client.delete_list(3) is None
    assert make_request.call_args.args == ("DELETE", f"{API}/lists/3")


async def test_add_item_omits_unset_optional_fields(mocker):
    """An omitted ``shopAreaId`` is what lets the app auto-detect the area."""
    client, make_request = _client(mocker, json_data=_ocs(ITEM_FIXTURE))

    await client.add_item(3, name="flour")

    assert make_request.call_args.kwargs["json"] == {"name": "flour", "checked": False}


async def test_add_item_sends_the_fields_it_was_given(mocker):
    client, make_request = _client(mocker, json_data=_ocs(ITEM_FIXTURE))

    item = await client.add_item(
        3, name="flour", quantity="2", unit="cups", shop_area_id=7, checked=True
    )

    assert item["id"] == 42
    assert make_request.call_args.args == ("POST", f"{API}/lists/3/items")
    assert make_request.call_args.kwargs["json"] == {
        "name": "flour",
        "checked": True,
        "quantity": "2",
        "unit": "cups",
        "shopAreaId": 7,
    }


async def test_update_item_sends_only_the_given_fields(mocker):
    """The app reads request params, so an absent key means "leave it alone"."""
    client, make_request = _client(mocker, json_data=_ocs(ITEM_FIXTURE))

    await client.update_item(3, 42, {"quantity": "3"})

    assert make_request.call_args.args == ("PUT", f"{API}/lists/3/items/42")
    assert make_request.call_args.kwargs["json"] == {"quantity": "3"}


async def test_check_item_hits_the_check_route(mocker):
    client, make_request = _client(
        mocker, json_data=_ocs({**ITEM_FIXTURE, "checked": True})
    )

    item = await client.check_item(3, 42, True)

    assert item["checked"] is True
    assert make_request.call_args.args == ("PUT", f"{API}/lists/3/items/42/check")
    assert make_request.call_args.kwargs["json"] == {"checked": True}


async def test_clear_checked_and_uncheck_all_use_their_own_routes(mocker):
    """The static routes must not be swallowed by ``/items/{id}``."""
    client, make_request = _client(mocker, content=b"")

    await client.clear_checked_items(3)
    assert make_request.call_args.args == ("DELETE", f"{API}/lists/3/items/checked")

    await client.uncheck_all_items(3)
    assert make_request.call_args.args == ("POST", f"{API}/lists/3/items/uncheck-all")
