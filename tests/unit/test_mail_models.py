"""Unit tests for Mail Pydantic models (alias mapping from the OCS API)."""

import pytest

from nextcloud_mcp_server.models.mail import (
    GetMessageResponse,
    ListAccountsResponse,
    MailAccount,
    MailMailbox,
    MailMessage,
    MailMessageSummary,
)

pytestmark = pytest.mark.unit


def test_account_maps_is_delegated_alias():
    account = MailAccount(**{"id": 1, "email": "a@example.com", "isDelegated": True})
    assert account.id == 1
    assert account.is_delegated is True


def test_mailbox_maps_camelcase_aliases():
    mailbox = MailMailbox(
        **{
            "databaseId": 10,
            "id": "SU5CT1g=",
            "name": "INBOX",
            "displayName": "Inbox",
            "accountId": 1,
            "specialUse": ["inbox"],
            "unread": 5,
        }
    )
    assert mailbox.database_id == 10
    assert mailbox.account_id == 1
    assert mailbox.display_name == "Inbox"
    assert mailbox.special_use == ["inbox"]
    assert mailbox.unread == 5


def test_message_summary_maps_from_and_dateint():
    summary = MailMessageSummary(
        **{
            "databaseId": 100,
            "subject": "Hello",
            "dateInt": 1700000000,
            "from": [{"label": "Alice", "email": "alice@example.com"}],
            "to": [{"email": "bob@example.com"}],
            "mailboxId": 10,
            "previewText": "snippet",
            "flags": {"seen": True, "hasAttachments": True},
        }
    )
    assert summary.database_id == 100
    assert summary.date_int == 1700000000
    assert summary.from_[0].label == "Alice"
    assert summary.to[0].email == "bob@example.com"
    assert summary.mailbox_id == 10
    assert summary.preview_text == "snippet"
    assert summary.flags is not None
    assert summary.flags.seen is True
    assert summary.flags.has_attachments is True


def test_full_message_maps_body_and_attachments():
    message = MailMessage(
        **{
            "id": 100,
            "subject": "Hello",
            "hasHtmlBody": True,
            "body": "<p>Hi</p>",
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
    assert message.id == 100
    assert message.has_html_body is True
    assert message.body == "<p>Hi</p>"
    assert message.attachments[0].file_name == "doc.pdf"
    assert message.attachments[0].id == "1.2"


def test_message_tolerates_missing_optional_fields():
    """A 206 partial response may omit the body."""
    message = MailMessage(**{"id": 100})
    assert message.id == 100
    assert message.body is None
    assert message.has_html_body is False
    assert message.attachments == []


def test_response_models_wrap_results():
    resp = ListAccountsResponse(
        results=[MailAccount(id=1, email="a@example.com")], total_count=1
    )
    assert resp.success is True
    assert resp.total_count == 1
    assert resp.results[0].email == "a@example.com"

    msg_resp = GetMessageResponse(message=MailMessage(id=5, subject="Hi"))
    assert msg_resp.message.id == 5
