"""Unit tests for MailClient API methods.

Mail 5.x exposes two route families (see ``~/Software/mail/appinfo/routes.php``
and the module docstring of ``client/mail.py``):

- **Direct REST resource routes** under ``/apps/mail/api`` — ``/accounts``,
  ``/mailboxes``, ``/messages``, ``/messages/{id}/attachment/{id}``, ``/outbox``
  — return plain JSON or raw bytes and are CSRF-exempt via the
  ``OCS-APIRequest: true`` header (no ``requesttoken`` round-trip needed). These
  bare ``/apps/...`` paths are normalised to ``/index.php/apps/...`` by
  ``BaseNextcloudClient._resolve_url`` inside ``MailClient._make_request`` (which
  the assertions below capture pre-normalisation, since ``_make_request`` is
  mocked).
- **OCS routes** under ``/ocs/v2.php/apps/mail`` — ``/message/{id}`` and
  ``/message/{id}/raw`` — return the standard OCS envelope and work with Basic
  Auth alone. (Attachment downloads use the direct ``/api/messages/{id}/attachment/{id}``
  route, which returns raw file bytes; the OCS attachment route is unreliable —
  see GH #989.)

The ``_api_*`` tests therefore feed plain JSON; the OCS tests feed an enveloped
payload. Shapes are the real ones verified against a live Mail 5.x backend via
the GreenMail integration suite.
"""

import base64
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


async def test_list_accounts_unwraps_direct_payload(mocker):
    """list_accounts returns the plain JSON list from the direct REST route.

    Mail 5.x's ``/api/accounts`` returns the address as ``emailAddress`` (the
    real shape verified against a live backend); the client returns the raw
    dicts and the server layer maps them onto ``MailAccount``.
    """
    mock_response = create_mock_response(
        json_data=[
            {"id": 1, "emailAddress": "alice@example.com", "isDelegated": False},
            {"id": 2, "emailAddress": "bob@example.com", "isDelegated": False},
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
    assert accounts[0]["emailAddress"] == "alice@example.com"

    # Direct resource route; CSRF-exempt via the OCS-APIRequest header (no token).
    args, kwargs = mock_make_request.call_args
    assert args == ("GET", "/apps/mail/api/accounts")
    assert kwargs["headers"]["OCS-APIRequest"] == "true"
    assert "requesttoken" not in kwargs["headers"]


async def test_get_mailboxes_unwraps_account_envelope(mocker):
    """get_mailboxes unwraps the ``{...,"mailboxes":[...]}`` account envelope.

    Mail 5.x's ``/api/mailboxes`` returns the folders nested under a
    ``mailboxes`` key (not a bare list); the client must unwrap them.
    """
    mock_response = create_mock_response(
        json_data={
            "id": 1,
            "email": "alice@example.com",
            "delimiter": ".",
            "mailboxes": [
                {
                    "databaseId": 10,
                    "id": "SU5CT1g=",
                    "name": "INBOX",
                    "displayName": "INBOX",
                    "accountId": 1,
                    "specialUse": ["inbox"],
                    "unread": 3,
                }
            ],
        }
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
    assert args == ("GET", "/apps/mail/api/mailboxes")
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
    messages = await client.list_messages(
        10, cursor=42, search_filter="hello", limit=50, view="threaded"
    )

    assert len(messages) == 1
    assert messages[0]["databaseId"] == 100

    args, kwargs = mock_make_request.call_args
    assert args == ("GET", "/apps/mail/api/messages")
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


async def test_get_attachment_returns_base64_bytes(mocker):
    """get_attachment fetches the direct route and base64-encodes the raw bytes.

    The filename is recovered from the Content-Disposition header, the mime from
    Content-Type, and the size from the byte length (GH #989).
    """
    raw = b"%PDF-1.4 fake pdf bytes"
    mock_response = create_mock_response(
        content=raw,
        headers={
            "content-type": "application/pdf; charset=binary",
            "content-disposition": 'attachment; filename="doc.pdf"',
        },
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", return_value=mock_response
    )

    client = MailClient(mock_client, "testuser")
    attachment = await client.get_attachment(100, "1.2")

    assert attachment["name"] == "doc.pdf"
    assert attachment["mime"] == "application/pdf"
    assert attachment["size"] == len(raw)
    assert attachment["content"] == base64.b64encode(raw).decode("ascii")
    assert base64.b64decode(attachment["content"]) == raw

    args, _ = mock_make_request.call_args
    assert args == ("GET", "/apps/mail/api/messages/100/attachment/1.2")


async def test_get_attachment_synthesizes_name_without_disposition(mocker):
    """Without a Content-Disposition filename, a synthetic name is used."""
    mock_response = create_mock_response(
        content=b"data",
        headers={"content-type": "application/octet-stream"},
    )
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mocker.patch.object(MailClient, "_make_request", return_value=mock_response)

    client = MailClient(mock_client, "testuser")
    attachment = await client.get_attachment(100, "1.2")

    assert attachment["name"] == "attachment_100_1.2"
    assert attachment["mime"] == "application/octet-stream"


async def test_get_attachment_url_encodes_attachment_id(mocker):
    """A traversal-style attachment_id is percent-encoded in the URL path."""
    mock_response = create_mock_response(content=b"y")
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
        "/apps/mail/api/messages/100/attachment/..%2F..%2Fevil",
    )


async def test_empty_data_returns_empty_list(mocker):
    """A non-list direct payload degrades to an empty list for list endpoints."""
    mock_response = create_mock_response(json_data={})
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mocker.patch.object(MailClient, "_make_request", return_value=mock_response)

    client = MailClient(mock_client, "testuser")
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


async def test_send_message_two_step_flow(mocker):
    """send_message stages via POST /outbox then sends via POST /outbox/{id}.

    Guards the create-response shape assumption (``data.id``) and the two-step
    flow that the integration suite can only xfail (the GreenMail outbox send
    fails before the success path).
    """
    create_response = create_mock_response(json_data={"data": {"id": 42}})
    send_response = create_mock_response(status_code=204, content=b"")
    mock_make_request = mocker.patch.object(
        MailClient, "_make_request", side_effect=[create_response, send_response]
    )

    client = MailClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    result = await client.send_message(
        1, [{"email": "a@b.com", "label": "A"}], "Hi", "body"
    )

    assert result["success"] is True
    assert result["outbox_id"] == 42

    calls = mock_make_request.call_args_list
    assert calls[0].args == ("POST", "/apps/mail/api/outbox")
    assert calls[1].args == ("POST", "/apps/mail/api/outbox/42")


async def test_send_message_missing_outbox_id_raises(mocker):
    """A create response without ``data.id`` raises (so the server reports an error).

    Returning an error dict here would be discarded by the server tool, which
    would then report ``success=True`` on a covert failure.
    """
    create_response = create_mock_response(json_data={"unexpected": "shape"})
    mocker.patch.object(MailClient, "_make_request", return_value=create_response)

    client = MailClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    with pytest.raises(httpx.RequestError):
        await client.send_message(1, [{"email": "a@b.com", "label": "A"}], "Hi", "body")
