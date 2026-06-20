"""Unit tests for MailClient API methods."""

import logging
from typing import Any

import httpx
import pytest

from nextcloud_mcp_server.client.mail import MailClient
from tests.client.conftest import create_mock_response

logger = logging.getLogger(__name__)

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


def _ocs_response(data: Any, status_code: int = 200) -> httpx.Response:
    """Wrap a payload in the standard OCS envelope."""
    return create_mock_response(
        status_code=status_code,
        json_data={
            "ocs": {
                "meta": {"status": "ok", "statuscode": status_code, "message": "OK"},
                "data": data,
            }
        },
    )


async def test_list_accounts_unwraps_ocs_envelope(mocker):
    """list_accounts returns the ocs.data payload."""
    mock_response = _ocs_response(
        [
            {"id": 1, "email": "alice@example.com", "isDelegated": False},
            {"id": 2, "email": "bob@example.com", "isDelegated": False},
        ]
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", return_value=mock_response
    )

    client = MailClient(mock_client, "testuser")
    accounts = await client.list_accounts()

    assert len(accounts) == 2
    assert accounts[0]["id"] == 1
    assert accounts[0]["email"] == "alice@example.com"

    # Correct URL, OCS header, and format=json param.
    args, kwargs = mock_make_request.call_args
    assert args == ("GET", "/ocs/v2.php/apps/mail/api/account/list")
    assert kwargs["headers"]["OCS-APIRequest"] == "true"
    assert kwargs["params"]["format"] == "json"


async def test_get_mailboxes_passes_account_id(mocker):
    """get_mailboxes sends accountId and unwraps the list."""
    mock_response = _ocs_response(
        [
            {
                "databaseId": 10,
                "id": "SU5CT1g=",
                "name": "INBOX",
                "displayName": "INBOX",
                "accountId": 1,
                "specialUse": ["inbox"],
                "unread": 3,
            }
        ]
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", return_value=mock_response
    )

    client = MailClient(mock_client, "testuser")
    mailboxes = await client.get_mailboxes(account_id=1)

    assert len(mailboxes) == 1
    assert mailboxes[0]["databaseId"] == 10
    assert mailboxes[0]["specialUse"] == ["inbox"]

    args, kwargs = mock_make_request.call_args
    assert args == ("GET", "/ocs/v2.php/apps/mail/api/mailboxes")
    assert kwargs["params"]["accountId"] == 1


async def test_list_messages_builds_params(mocker):
    """list_messages forwards limit/cursor/filter/view query params."""
    mock_response = _ocs_response(
        [
            {
                "databaseId": 100,
                "subject": "Hello",
                "dateInt": 1700000000,
                "from": [{"label": "Alice", "email": "alice@example.com"}],
                "to": [{"label": "Bob", "email": "bob@example.com"}],
                "mailboxId": 10,
            }
        ]
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", return_value=mock_response
    )

    client = MailClient(mock_client, "testuser")
    messages = await client.list_messages(
        10, cursor=42, filter="hello", limit=50, view="threaded"
    )

    assert len(messages) == 1
    assert messages[0]["databaseId"] == 100

    args, kwargs = mock_make_request.call_args
    assert args == ("GET", "/ocs/v2.php/apps/mail/api/mailboxes/10/messages")
    assert kwargs["params"]["limit"] == 50
    assert kwargs["params"]["cursor"] == 42
    assert kwargs["params"]["filter"] == "hello"
    assert kwargs["params"]["view"] == "threaded"


async def test_list_messages_omits_optional_params(mocker):
    """Optional params are omitted when not supplied; limit always present."""
    mock_response = _ocs_response([])
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", return_value=mock_response
    )

    client = MailClient(mock_client, "testuser")
    await client.list_messages(10)

    _, kwargs = mock_make_request.call_args
    params = kwargs["params"]
    assert params["limit"] == 20  # default
    assert "cursor" not in params
    assert "filter" not in params
    assert "view" not in params


async def test_get_message_unwraps_full_message(mocker):
    """get_message returns the full message dict."""
    mock_response = _ocs_response(
        {
            "id": 100,
            "subject": "Hello",
            "hasHtmlBody": True,
            "body": "<p>Hi there</p>",
            "from": [{"label": "Alice", "email": "alice@example.com"}],
            "attachments": [
                {
                    "id": "1.2",
                    "fileName": "doc.pdf",
                    "mime": "application/pdf",
                    "size": 1024,
                }
            ],
        }
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", return_value=mock_response
    )

    client = MailClient(mock_client, "testuser")
    message = await client.get_message(100)

    assert message["id"] == 100
    assert message["hasHtmlBody"] is True
    assert message["attachments"][0]["fileName"] == "doc.pdf"

    args, _ = mock_make_request.call_args
    assert args == ("GET", "/ocs/v2.php/apps/mail/api/message/100")


async def test_get_attachment_unwraps_json(mocker):
    """get_attachment returns the JSON attachment object (not a binary download)."""
    mock_response = _ocs_response(
        {"name": "doc.pdf", "mime": "application/pdf", "size": 1024, "content": "abc"}
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", return_value=mock_response
    )

    client = MailClient(mock_client, "testuser")
    attachment = await client.get_attachment(100, "1.2")

    assert attachment["name"] == "doc.pdf"
    assert attachment["content"] == "abc"

    args, _ = mock_make_request.call_args
    assert args == ("GET", "/ocs/v2.php/apps/mail/api/message/100/attachment/1.2")


async def test_empty_data_returns_empty_list(mocker):
    """A null ocs.data payload degrades to an empty list for list endpoints."""
    mock_response = _ocs_response(None)
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mocker.patch.object(MailClient, "_make_request", return_value=mock_response)

    client = MailClient(mock_client, "testuser")
    assert await client.list_accounts() == []
