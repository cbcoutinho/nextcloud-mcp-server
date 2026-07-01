"""End-to-end Mail integration tests against a live GreenMail + Nextcloud stack.

These exercise the mail MCP tools through the single-user `mcp` service
(http://localhost:8000) against a real Nextcloud `app` with the Mail app
installed and a GreenMail-backed account provisioned for the `admin` test user.

Why this suite exists (#965 follow-up): the Mail 5.x *direct REST* routes
(`/index.php/apps/mail/api/{accounts,mailboxes,messages,outbox}`) are CSRF-gated
(regular non-OCS controllers, no `@NoCSRFRequired`), while `NextcloudClient`
strips `Set-Cookie` (`AsyncDisableCookieTransport`) and authenticates per-request
with an App Password — so it cannot maintain the session a CSRF `requesttoken`
needs. This suite proves, end-to-end, whether the mail feature actually works
from the MCP server before we decide to keep or remove it.

Requires the `mail` + `single-user` compose profiles:

    docker compose --profile mail --profile single-user up --build -d
    scripts/provision-greenmail-account.sh admin admin@example.org
    uv run pytest -m integration tests/integration/test_mail_greenmail.py -v
"""

import base64
import json
import smtplib
import subprocess
from email.message import EmailMessage
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVISION_SCRIPT = _REPO_ROOT / "scripts" / "provision-greenmail-account.sh"

ADMIN_EMAIL = "admin@example.org"


def _tool_payload(result) -> dict:
    """Extract the JSON body returned by an MCP tool call.

    Tools return a Pydantic ``BaseResponse`` serialized as the first text
    content block; ``isError`` results raise so the test fails loudly with the
    server-side error (which, for a CSRF rejection, is the evidence we want).
    """
    if getattr(result, "isError", False):
        text = result.content[0].text if result.content else "<no content>"
        raise AssertionError(f"MCP tool returned an error: {text}")
    return json.loads(result.content[0].text)


@pytest.fixture(scope="module")
def provisioned_mail_account() -> str:
    """Ensure a GreenMail-backed mail account exists for `admin`.

    Skips the whole module if provisioning fails (e.g. the `mail`/`single-user`
    profiles aren't running), so the suite is a no-op outside the mail CI lane.
    """
    if not _PROVISION_SCRIPT.exists():
        pytest.skip("provision-greenmail-account.sh missing")
    proc = subprocess.run(
        ["bash", str(_PROVISION_SCRIPT), "admin", ADMIN_EMAIL],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(
            "GreenMail mail account provisioning failed (mail/single-user "
            f"profiles up?):\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return ADMIN_EMAIL


def _seed_inbox_message(subject: str, body: str) -> None:
    """Deliver a multipart message to the admin mailbox via GreenMail SMTP (3025).

    The message is deliberately ``multipart/alternative`` (plain + HTML). GreenMail
    2.1.x has a bug where an IMAP ``BODY`` fetch on a *non*-multipart (plain
    text/plain) message throws ``ClassCastException`` (String → MimeMultipart),
    which Nextcloud Mail surfaces as "Could not connect to IMAP server" on
    ``get_message``. A multipart body sidesteps that GreenMail defect; real IMAP
    servers handle either shape.
    """
    msg = EmailMessage()
    msg["From"] = "sender@example.org"
    msg["To"] = ADMIN_EMAIL
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_alternative(f"<p>{body}</p>", subtype="html")
    with smtplib.SMTP("localhost", 3025, timeout=15) as smtp:
        smtp.send_message(msg)


def _seed_message_with_attachment(
    subject: str,
    attachment: bytes,
    filename: str,
    *,
    maintype: str = "text",
    subtype: str = "plain",
) -> None:
    """Deliver a multipart/mixed message carrying a file attachment via SMTP.

    ``maintype``/``subtype`` default to ``text/plain``; pass e.g.
    ``application``/``pdf`` to attach a binary file.
    """
    msg = EmailMessage()
    msg["From"] = "sender@example.org"
    msg["To"] = ADMIN_EMAIL
    msg["Subject"] = subject
    msg.set_content("see attached")
    msg.add_alternative("<p>see attached</p>", subtype="html")
    msg.add_attachment(
        attachment, maintype=maintype, subtype=subtype, filename=filename
    )
    with smtplib.SMTP("localhost", 3025, timeout=15) as smtp:
        smtp.send_message(msg)


def _minimal_pdf_bytes() -> bytes:
    """A tiny but structurally valid single-page PDF (with a ``%PDF`` signature).

    Built inline (no fixture file) so the binary-attachment roundtrip test is
    self-contained. Exercises the non-text path that base64-encodes raw bytes.
    """
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>\nendobj\n",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for obj in objects:
        offsets.append(len(pdf))
        pdf += obj
    xref_pos = len(pdf)
    pdf += b"xref\n0 %d\n" % (len(objects) + 1)
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    pdf += b"startxref\n%d\n%%%%EOF\n" % xref_pos
    return pdf


def _sync_mail_account(account_id: int) -> None:
    """Force Nextcloud Mail to sync the IMAP account so seeded mail is visible."""
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "php",
            "/var/www/html/occ",
            "mail:account:sync",
            str(account_id),
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Surface a sync failure so a later "message not found" assertion reads
        # as a sync problem, not a client bug.
        print(
            f"WARNING: mail:account:sync failed (rc={proc.returncode}): {proc.stderr}"
        )


async def _first_account_id(nc_mcp_client) -> int:
    data = _tool_payload(await nc_mcp_client.call_tool("nc_mail_list_accounts", {}))
    accounts = data["results"]
    assert accounts, "no mail accounts returned"
    return accounts[0]["id"]


async def _sync_and_fetch_message(nc_mcp_client, subject: str) -> dict:
    """Sync the account, locate the seeded message by subject, return its full body.

    Drives list_mailboxes → list_messages → get_message through the MCP tools.
    """
    account_id = await _first_account_id(nc_mcp_client)
    _sync_mail_account(account_id)

    mailboxes = _tool_payload(
        await nc_mcp_client.call_tool(
            "nc_mail_list_mailboxes", {"account_id": account_id}
        )
    )["results"]
    inbox = next(m for m in mailboxes if m["name"].upper() == "INBOX")

    # Newest-first ordering means the just-seeded message is on the first page,
    # so limit=20 finds it even on a persistent INBOX with >20 older messages.
    messages = _tool_payload(
        await nc_mcp_client.call_tool(
            "nc_mail_list_messages", {"mailbox_id": inbox["databaseId"], "limit": 20}
        )
    )["results"]
    match = next((m for m in messages if m["subject"] == subject), None)
    assert match is not None, f"seeded message {subject!r} not found in {messages}"

    return _tool_payload(
        await nc_mcp_client.call_tool(
            "nc_mail_get_message", {"message_id": match["databaseId"]}
        )
    )["message"]


async def test_list_accounts_returns_provisioned_account(
    nc_mcp_client, provisioned_mail_account
):
    """The decisive CSRF test: list the provisioned account via the MCP server.

    If the direct REST route is CSRF-gated for our cookieless App-Password
    client, this call fails — which is the evidence that the mail feature does
    not work from the MCP server.
    """
    data = _tool_payload(await nc_mcp_client.call_tool("nc_mail_list_accounts", {}))
    # Responses serialize by alias (e.g. mailboxes expose ``databaseId``), so the
    # account address comes back under the Mail API's ``emailAddress`` key.
    emails = [a["emailAddress"] for a in data["results"]]
    assert provisioned_mail_account in emails


async def test_list_mailboxes(nc_mcp_client, provisioned_mail_account):
    """List mailboxes for the provisioned account (expects at least INBOX)."""
    account_id = await _first_account_id(nc_mcp_client)
    data = _tool_payload(
        await nc_mcp_client.call_tool(
            "nc_mail_list_mailboxes", {"account_id": account_id}
        )
    )
    names = {m["name"].upper() for m in data["results"]}
    assert "INBOX" in names


async def test_list_and_get_message_roundtrip(nc_mcp_client, provisioned_mail_account):
    """Seed a message via SMTP, sync, then list + fetch it through the MCP tools."""
    subject = "GreenMail integration probe"
    _seed_inbox_message(subject, "hello from greenmail")

    full = await _sync_and_fetch_message(nc_mcp_client, subject)
    assert full["subject"] == subject


async def test_get_attachment_roundtrip(nc_mcp_client, provisioned_mail_account):
    """Seed a message with a file attachment, then fetch it through the MCP tools."""
    subject = "Attachment integration probe"
    payload = b"hello attachment contents\n"
    _seed_message_with_attachment(subject, payload, "note.txt")

    full = await _sync_and_fetch_message(nc_mcp_client, subject)
    attachments = full["attachments"]
    assert attachments, f"no attachments on message {full}"
    att = attachments[0]
    assert att["fileName"] == "note.txt"

    fetched = _tool_payload(
        await nc_mcp_client.call_tool(
            "nc_mail_get_attachment",
            {"message_id": full["id"], "attachment_id": att["id"]},
        )
    )
    assert fetched["name"] == "note.txt"
    # The direct attachment route returns raw bytes, which the client always
    # base64-encodes (GH #989), so decode the base64 to recover the payload.
    decoded = base64.b64decode(fetched["content"]).decode("utf-8")
    assert payload.decode("utf-8").strip() in decoded


async def test_get_pdf_attachment_roundtrip(nc_mcp_client, provisioned_mail_account):
    """Fetch a binary (PDF) attachment end-to-end via the direct route (GH #989).

    Complements the text-attachment test: PDF bytes are not valid UTF-8, so this
    proves the direct route + base64 encoding round-trips arbitrary binary data
    (not just text) and preserves it byte-for-byte, including the ``%PDF``
    signature. Runs against the single-user / BasicAuth MCP server — the same
    deployment mode as the original bug report.
    """
    subject = "PDF attachment integration probe"
    payload = _minimal_pdf_bytes()
    assert payload.startswith(b"%PDF")
    _seed_message_with_attachment(
        subject, payload, "sample.pdf", maintype="application", subtype="pdf"
    )

    full = await _sync_and_fetch_message(nc_mcp_client, subject)
    attachments = full["attachments"]
    assert attachments, f"no attachments on message {full}"
    att = attachments[0]
    assert att["fileName"] == "sample.pdf"

    fetched = _tool_payload(
        await nc_mcp_client.call_tool(
            "nc_mail_get_attachment",
            {"message_id": full["id"], "attachment_id": att["id"]},
        )
    )
    assert fetched["name"] == "sample.pdf"
    assert (fetched["mime"] or "").lower() == "application/pdf"
    # Decode base64 and confirm the raw bytes survived intact (binary-safe).
    decoded = base64.b64decode(fetched["content"])
    assert decoded == payload
    assert decoded.startswith(b"%PDF")
    assert fetched["size"] == len(payload)


@pytest.mark.xfail(
    reason="Mail outbox send fails internally (HTTP 500, before any SMTP "
    "connection) for a CLI-provisioned GreenMail account — the sender identity "
    "isn't resolved (stored row has from:[]). This is a Mail-app/test-env "
    "limitation, not the MCP client: the client implements the two-step "
    "create+send per OutboxController::create, and the create step succeeds.",
    strict=False,
)
async def test_send_message_via_outbox(nc_mcp_client, provisioned_mail_account):
    """Send a message through the Mail outbox (create succeeds; SMTP send is env-limited)."""
    account_id = await _first_account_id(nc_mcp_client)
    subject = "Sent from MCP via outbox"
    # The tool takes `to` as a JSON-array string; From is derived from account_id.
    data = _tool_payload(
        await nc_mcp_client.call_tool(
            "nc_mail_send_message",
            {
                "account_id": account_id,
                "to": json.dumps(
                    [{"email": "recipient@example.org", "label": "Recipient"}]
                ),
                "subject": subject,
                "body": "body sent through the Nextcloud Mail outbox API",
            },
        )
    )
    assert data["success"] is True
