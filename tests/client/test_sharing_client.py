"""Unit tests for SharingClient — wire-format checks for the OCS Sharing API.

These verify the payload shape sent to Nextcloud. Coverage includes:

- ``shareType=12`` (``IShare::TYPE_DECK``), which powers Deck card file
  attachments — the Deck UI fires this exact request (see
  ``~/Software/deck/src/components/card/AttachmentList.vue:223-238``).
- ``shareType=3`` public download links (``create_public_link``), including
  the ``expireDate`` passthrough and the OCS error/empty-data branches.
"""

import pytest
from httpx import AsyncClient

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
    sharing_client._client.post.return_value = _ok_share_response(mocker, share_id=99)

    share = await sharing_client.create_share(
        path="/Notes/My Note.md",
        share_with="123",
        share_type=12,
        permissions=1,
    )

    assert share["id"] == 99
    sharing_client._client.post.assert_called_once()
    call = sharing_client._client.post.call_args
    assert call.args[0] == "/ocs/v2.php/apps/files_sharing/api/v1/shares"
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
    sharing_client._client.post.return_value = _ok_share_response(
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
    sharing_client._client.post.assert_called_once()
    call = sharing_client._client.post.call_args
    assert call.args[0] == "/ocs/v2.php/apps/files_sharing/api/v1/shares"
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
    sharing_client._client.post.return_value = _ok_share_response(
        mocker, share_id=8, url="https://nc.example.com/s/noexpiry"
    )

    await sharing_client.create_public_link(path="/doc.pdf")

    call = sharing_client._client.post.call_args
    assert "expireDate" not in call.kwargs["data"]
    assert call.kwargs["data"]["shareType"] == 3


async def test_create_public_link_raises_on_empty_data(sharing_client, mocker):
    """An OK status with empty data means the link was not created."""
    response = mocker.Mock()
    response.raise_for_status = mocker.Mock()
    response.json.return_value = {
        "ocs": {"meta": {"statuscode": 200, "message": "OK"}, "data": []}
    }
    sharing_client._client.post.return_value = response

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
    sharing_client._client.post.return_value = response

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
    sharing_client._client.post.return_value = response

    with pytest.raises(RuntimeError, match="Wrong path"):
        await sharing_client.create_share(
            path="/nope.md",
            share_with="1",
            share_type=12,
        )
