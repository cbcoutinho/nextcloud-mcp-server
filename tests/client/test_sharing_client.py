"""Unit tests for SharingClient — wire-format checks for the OCS Sharing API.

These verify the payload shape sent to Nextcloud. Coverage includes:

- ``shareType=12`` (``IShare::TYPE_DECK``), which powers Deck card file
  attachments — the Deck UI fires this exact request (see
  ``~/Software/deck/src/components/card/AttachmentList.vue:223-238``).
- ``shareType=3`` public download links (``create_public_link``), including
  the ``expireDate`` passthrough and the OCS error/empty-data branches.
"""

import pytest
from httpx import AsyncClient, HTTPStatusError, Request

from nextcloud_mcp_server.client.sharing import SharingClient

pytestmark = pytest.mark.unit


@pytest.fixture
def sharing_client(mocker):
    """SharingClient with a mocked underlying httpx client."""
    mock_http = mocker.AsyncMock(spec=AsyncClient)
    return SharingClient(mock_http, "testuser")


def _ok_share_response(mocker, share_id: int = 4242, **extra):
    """Build a fake OCS create-share success response."""
    response = mocker.Mock()
    response.raise_for_status = mocker.Mock()
    response.json.return_value = {
        "ocs": {
            "meta": {"statuscode": 200, "message": "OK"},
            "data": {"id": share_id, **extra},
        }
    }
    return response


async def test_create_share_deck_type_payload(sharing_client, mocker):
    """create_share(share_type=12) must POST exactly what the Deck UI does:
    {path, shareType: 12, shareWith: "<cardId>"} to /ocs/v2.php/apps/files_sharing/api/v1/shares.

    Drift here would silently break Deck attachments — Nextcloud's
    ShareAPIController routes shareType=12 to DeckShareProvider, which
    creates the deck-card share row binding the file to the card.
    """
    sharing_client._client.request.return_value = _ok_share_response(
        mocker, share_id=99
    )

    share = await sharing_client.create_share(
        path="/Notes/My Note.md",
        share_with="123",
        share_type=12,
        permissions=1,
    )

    assert share["id"] == 99
    sharing_client._client.request.assert_called_once()
    call = sharing_client._client.request.call_args
    assert call.args[:2] == (
        "POST",
        "/ocs/v2.php/apps/files_sharing/api/v1/shares",
    )
    assert call.kwargs["data"] == {
        "path": "/Notes/My Note.md",
        "shareType": 12,
        "shareWith": "123",
        "permissions": 1,
    }
    # Nextcloud demands this header on OCS endpoints; without it the request
    # is rejected as a CSRF risk.
    assert call.kwargs["headers"]["OCS-APIRequest"] == "true"


async def test_create_public_link_payload(sharing_client, mocker):
    """create_public_link must POST shareType=3 with no shareWith, and pass
    through expireDate when supplied. Public link data carries url + token."""
    sharing_client._client.request.return_value = _ok_share_response(
        mocker,
        share_id=7,
        url="https://nc.example.com/s/abc123",
        token="abc123",
        permissions=1,
    )

    share = await sharing_client.create_public_link(
        path="/Receipts/receipt.jpg",
        permissions=1,
        expire_date="2026-06-25",
    )

    # This layer only returns the raw OCS payload; expires_at/download_url are
    # derived at the tool layer (covered in tests/unit/server).
    assert share["id"] == 7
    assert share["url"] == "https://nc.example.com/s/abc123"
    assert share["token"] == "abc123"
    sharing_client._client.request.assert_called_once()
    call = sharing_client._client.request.call_args
    assert call.args[:2] == (
        "POST",
        "/ocs/v2.php/apps/files_sharing/api/v1/shares",
    )
    assert call.kwargs["data"] == {
        "path": "/Receipts/receipt.jpg",
        "shareType": 3,
        "permissions": 1,
        "expireDate": "2026-06-25",
    }
    # Public link: no recipient is sent.
    assert "shareWith" not in call.kwargs["data"]
    assert call.kwargs["headers"]["OCS-APIRequest"] == "true"


async def test_create_public_link_omits_expire_date_when_none(sharing_client, mocker):
    """When no expiry is given, expireDate must be absent from the payload."""
    sharing_client._client.request.return_value = _ok_share_response(
        mocker, share_id=8, url="https://nc.example.com/s/noexpiry"
    )

    await sharing_client.create_public_link(path="/doc.pdf")

    call = sharing_client._client.request.call_args
    assert "expireDate" not in call.kwargs["data"]
    assert call.kwargs["data"]["shareType"] == 3


async def test_create_public_link_raises_on_empty_data(sharing_client, mocker):
    """An OK status with empty data means the link was not created."""
    response = mocker.Mock()
    response.raise_for_status = mocker.Mock()
    response.json.return_value = {
        "ocs": {"meta": {"statuscode": 200, "message": "OK"}, "data": []}
    }
    sharing_client._client.request.return_value = response

    with pytest.raises(RuntimeError, match="Public link creation failed"):
        await sharing_client.create_public_link(path="/missing.jpg")


async def test_create_public_link_raises_on_ocs_error(sharing_client, mocker):
    """A non-100/200 OCS statuscode raises RuntimeError with the OCS message."""
    response = mocker.Mock()
    response.raise_for_status = mocker.Mock()
    response.json.return_value = {
        "ocs": {
            "meta": {
                "statuscode": 404,
                "message": "Wrong path, file/folder doesn't exist",
            },
            "data": [],
        }
    }
    sharing_client._client.request.return_value = response

    with pytest.raises(RuntimeError, match="Wrong path"):
        await sharing_client.create_public_link(path="/nope.jpg")


async def test_create_share_raises_on_ocs_failure(sharing_client, mocker):
    """OCS error responses (statuscode != 100/200) raise RuntimeError."""
    response = mocker.Mock()
    response.raise_for_status = mocker.Mock()
    response.json.return_value = {
        "ocs": {
            "meta": {
                "statuscode": 404,
                "message": "Wrong path, file/folder doesn't exist",
            },
            "data": [],
        }
    }
    sharing_client._client.request.return_value = response

    with pytest.raises(RuntimeError, match="Wrong path"):
        await sharing_client.create_share(
            path="/nope.md",
            share_with="1",
            share_type=12,
        )


async def test_create_share_rejects_public_link_with_recipient(sharing_client):
    """A public link carrying a recipient must be refused before the request.

    Nextcloud accepts this pairing and silently ignores ``shareWith``, handing
    back a valid anonymous link. The caller is told the share succeeded and
    believes the file went to the named user, when it was actually published to
    anyone holding the URL -- so the check has to happen client-side.
    """
    with pytest.raises(ValueError, match="must not carry shareWith"):
        await sharing_client.create_share(
            path="/Secrets/salaries.xlsx",
            share_with="alice",
            share_type=3,
        )

    # Refused before the wire: nothing was sent.
    sharing_client._client.request.assert_not_called()


@pytest.mark.parametrize("recipient", [None, "", "   "])
async def test_create_share_requires_recipient_for_user_share(
    sharing_client, recipient
):
    """A recipient-typed share with no usable recipient is refused locally.

    The server does reject this, but as a generic OCS 400 that names neither
    the field nor what belongs in it. Blank and whitespace-only are treated the
    same as absent.
    """
    with pytest.raises(ValueError, match="requires a non-empty shareWith"):
        await sharing_client.create_share(
            path="/Documents/report.md",
            share_with=recipient,
            share_type=0,
        )

    sharing_client._client.request.assert_not_called()


async def test_create_share_public_link_omits_share_with(sharing_client, mocker):
    """A recipient-less public link is allowed and sends no shareWith field."""
    sharing_client._client.request.return_value = _ok_share_response(
        mocker, share_id=11
    )

    await sharing_client.create_share(
        path="/Public/flyer.pdf",
        share_type=3,
    )

    assert sharing_client._client.request.call_args.kwargs["data"] == {
        "path": "/Public/flyer.pdf",
        "shareType": 3,
        "permissions": 1,
    }


async def test_create_share_allows_unknown_share_type_with_recipient(
    sharing_client, mocker
):
    """An unrecognised share type is passed through, not rejected.

    Nextcloud may add share types we do not know about; refusing them here
    would break an otherwise-correct caller. Only the recipient rule applies.
    """
    sharing_client._client.request.return_value = _ok_share_response(
        mocker, share_id=12
    )

    await sharing_client.create_share(
        path="/Documents/report.md",
        share_with="some-identifier",
        share_type=99,
    )

    assert sharing_client._client.request.call_args.kwargs["data"]["shareType"] == 99


async def test_sharing_calls_are_metered_like_every_other_client(
    sharing_client, mocker
):
    """A sharing call must reach the shared API-call metric.

    Before this client routed through ``_make_request`` it issued
    ``self._client.post(...)`` directly, so its six endpoints were the only app
    calls in the codebase that produced no ``mcp_nextcloud_api_requests_total``
    sample and no ``trace_nextcloud_api_call`` span -- invisible in exactly the
    dashboards that exist to spot a failing Nextcloud.
    """
    record = mocker.patch("nextcloud_mcp_server.client.base.record_nextcloud_api_call")
    sharing_client._client.request.return_value = _ok_share_response(mocker)

    await sharing_client.list_shares()

    record.assert_called_once()
    assert record.call_args.kwargs["app"] == "sharing"
    assert record.call_args.kwargs["method"] == "GET"


async def test_rate_limit_retry_budget_survives_the_migration(sharing_client, mocker):
    """A 429 must still cost 5 attempts, now that the retry lives one layer down.

    These methods each carried their own ``@retry_on_429`` before routing
    through ``_make_request``; the decorator on ``_make_request`` is now the
    only one. That is a behaviour-preserving swap rather than a fix: nesting
    the two could never have multiplied the budget, because the inner loop
    reports exhaustion as ``RuntimeError`` and ``retry_on_429`` only catches
    ``HTTPStatusError``. What this pins is that dropping the per-method copy
    did not leave the calls with no retry at all. The mocked sleep keeps it
    fast.
    """
    sleep = mocker.patch("nextcloud_mcp_server.client.base.anyio.sleep")

    response = mocker.Mock()
    response.status_code = 429
    response.raise_for_status.side_effect = lambda: (_ for _ in ()).throw(
        HTTPStatusError("429", request=Request("GET", "http://x"), response=response)
    )
    sharing_client._client.request.return_value = response

    with pytest.raises(RuntimeError, match="Maximum number of retries"):
        await sharing_client.list_shares()

    assert sharing_client._client.request.call_count == 5
    assert sleep.call_count == 5
