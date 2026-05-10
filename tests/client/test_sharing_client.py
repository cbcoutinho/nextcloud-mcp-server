"""Unit tests for SharingClient — wire-format checks for the OCS Sharing API.

These verify the payload shape sent to Nextcloud, particularly for
``shareType=12`` (``IShare::TYPE_DECK``), which is what powers Deck card
file attachments. The Deck UI fires this exact request — see
``~/Software/deck/src/components/card/AttachmentList.vue:223-238``.
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
