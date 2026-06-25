"""Unit tests for MailClient API methods.

Mail 5.x exposes two route families (see ``~/Software/mail/appinfo/routes.php``
and the module docstring of ``client/mail.py``):

- **Direct REST resource routes** under ``/index.php/apps/mail/api`` —
  ``/accounts``, ``/mailboxes``, ``/messages``, ``/outbox`` — return the payload
  *unwrapped* (plain JSON list/dict) and require a CSRF ``requesttoken`` header.
- **OCS routes** under ``/ocs/v2.php/apps/mail`` — ``/message/{id}``,
  ``/message/{id}/attachment/{id}``, ``/message/{id}/raw`` — return the standard
  OCS envelope and work with Basic Auth alone.

The ``_api_*`` tests therefore feed plain JSON and stub the CSRF round-trip; the
OCS tests feed an enveloped payload.
"""

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
    """Wrap a payload in the standard OCS envelope (for OCS-route tests)."""
    return create_mock_response(
        status_code=status_code,
        json_data={
            "ocs": {
                "meta": {"status": "ok", "statuscode": status_code, "message": "OK"},
                "data": data,
            }
        },
    )


def _stub_csrf(mocker, client: MailClient) -> None:
    """Bypass the CSRF round-trip for direct-API tests.

    The direct REST routes call ``_ensure_csrf`` (a live GET to
    ``/index.php/csrftoken`` via the raw http client) before each request. Unit
    tests patch ``_make_request`` only, so stub the token acquisition out and
    pre-seed a token so the ``requesttoken`` header is populated.
    """
    mocker.patch.object(MailClient, "_ensure_csrf", new=mocker.AsyncMock())
    client._request_token = "test-token"


async def test_list_accounts_unwraps_direct_payload(mocker):
    """list_accounts returns the plain JSON list from the direct REST route."""
    mock_response = create_mock_response(
        json_data=[
            {"id": 1, "email": "alice@example.com", "isDelegated": False},
            {"id": 2, "email": "bob@example.com", "isDelegated": False},
        ]
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", return_value=mock_response
    )

    client = MailClient(mock_client, "testuser")
    _stub_csrf(mocker, client)
    accounts = await client.list_accounts()

    assert len(accounts) == 2
    assert accounts[0]["id"] == 1
    assert accounts[0]["email"] == "alice@example.com"

    # Direct resource route + CSRF header.
    args, kwargs = mock_make_request.call_args
    assert args == ("GET", "/index.php/apps/mail/api/accounts")
    assert kwargs["headers"]["requesttoken"] == "test-token"


async def test_get_mailboxes_passes_account_id(mocker):
    """get_mailboxes sends accountId and unwraps the list."""
    mock_response = create_mock_response(
        json_data=[
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
    _stub_csrf(mocker, client)
    mailboxes = await client.get_mailboxes(account_id=1)

    assert len(mailboxes) == 1
    assert mailboxes[0]["databaseId"] == 10
    assert mailboxes[0]["specialUse"] == ["inbox"]

    args, kwargs = mock_make_request.call_args
    assert args == ("GET", "/index.php/apps/mail/api/mailboxes")
    assert kwargs["params"]["accountId"] == 1


async def test_list_messages_builds_params(mocker):
    """list_messages forwards mailboxId/limit/cursor/filter/view query params."""
    mock_response = create_mock_response(
        json_data=[
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
    _stub_csrf(mocker, client)
    messages = await client.list_messages(
        10, cursor=42, search_filter="hello", limit=50, view="threaded"
    )

    assert len(messages) == 1
    assert messages[0]["databaseId"] == 100

    args, kwargs = mock_make_request.call_args
    assert args == ("GET", "/index.php/apps/mail/api/messages")
    assert kwargs["params"]["mailboxId"] == 10
    assert kwargs["params"]["limit"] == 50
    assert kwargs["params"]["cursor"] == 42
    assert kwargs["params"]["filter"] == "hello"
    assert kwargs["params"]["view"] == "threaded"


async def test_list_messages_omits_optional_params(mocker):
    """Optional params are omitted when not supplied; mailboxId/limit always present."""
    mock_response = create_mock_response(json_data=[])
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", return_value=mock_response
    )

    client = MailClient(mock_client, "testuser")
    _stub_csrf(mocker, client)
    await client.list_messages(10)

    _, kwargs = mock_make_request.call_args
    params = kwargs["params"]
    assert params["mailboxId"] == 10
    assert params["limit"] == 20  # default
    assert "cursor" not in params
    assert "filter" not in params
    assert "view" not in params


async def test_get_message_unwraps_full_message(mocker):
    """get_message returns the full message dict from the OCS route."""
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
    assert args == ("GET", "/ocs/v2.php/apps/mail/message/100")


async def test_get_attachment_unwraps_json(mocker):
    """get_attachment returns the JSON attachment object from the OCS route."""
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
    assert args == ("GET", "/ocs/v2.php/apps/mail/message/100/attachment/1.2")


async def test_get_attachment_url_encodes_attachment_id(mocker):
    """A traversal-style attachment_id is percent-encoded in the URL path."""
    mock_response = _ocs_response({"name": "x", "content": "y"})
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", return_value=mock_response
    )

    client = MailClient(mock_client, "testuser")
    await client.get_attachment(100, "../../evil")

    args, _ = mock_make_request.call_args
    # The "/" and ".." are encoded, so they can't escape the attachment path.
    assert args == (
        "GET",
        "/ocs/v2.php/apps/mail/message/100/attachment/..%2F..%2Fevil",
    )


async def test_empty_data_returns_empty_list(mocker):
    """A non-list direct payload degrades to an empty list for list endpoints."""
    mock_response = create_mock_response(json_data={})
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mocker.patch.object(MailClient, "_make_request", return_value=mock_response)

    client = MailClient(mock_client, "testuser")
    _stub_csrf(mocker, client)
    assert await client.list_accounts() == []


async def test_ocs_meta_failure_raises_httpstatuserror(mocker):
    """HTTP 200 with an OCS meta failure code is re-raised as HTTPStatusError.

    The synthetic response carries the OCS statuscode so callers' 404/403
    handling applies (e.g. nc_mail_get_message maps 404 to 'not found').
    """
    mock_response = create_mock_response(
        status_code=200,
        json_data={
            "ocs": {
                "meta": {"status": "failure", "statuscode": 404, "message": "nope"},
                "data": None,
            }
        },
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mocker.patch.object(MailClient, "_make_request", return_value=mock_response)

    client = MailClient(mock_client, "testuser")
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.get_message(100)
    assert excinfo.value.response.status_code == 404


async def test_non_json_response_raises_requesterror(mocker):
    """A non-JSON 200 body on an OCS route (Mail app absent) raises RequestError."""
    mock_response = create_mock_response(
        status_code=200, content=b"<html>not found</html>"
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mocker.patch.object(MailClient, "_make_request", return_value=mock_response)

    client = MailClient(mock_client, "testuser")
    with pytest.raises(httpx.RequestError):
        await client.get_message(100)
