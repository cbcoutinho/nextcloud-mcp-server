"""Client for Nextcloud Mail app operations (read-only).

Talks to the Mail app's OCS API under ``/ocs/v2.php/apps/mail/api/...``. The
Mail app's *server* handles the IMAP connection on the user's behalf, so this
client only ever speaks HTTP to Nextcloud — it never connects to IMAP/POP3
itself. The read endpoints are ``#[NoCSRFRequired]`` + ``#[NoAdminRequired]``,
so they are reachable with the same Basic-Auth app-password flow the other app
clients use, provided the ``OCS-APIRequest`` header is sent.

Prerequisites: the mail account must already be configured inside the Nextcloud
Mail app (so the server has IMAP credentials), and the Mail app must expose the
OCS API controllers (Mail 5.x / Nextcloud 32+).
"""

import logging
from typing import Any

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)


class MailClient(BaseNextcloudClient):
    """Read-only client for Nextcloud Mail app operations."""

    app_name = "mail"
    API_BASE = "/ocs/v2.php/apps/mail/api"

    # OCS endpoints require this header; without it Nextcloud rejects the
    # request (or redirects to a login page). ``format=json`` forces a JSON
    # envelope rather than XML.
    _OCS_HEADERS = {"OCS-APIRequest": "true", "Accept": "application/json"}

    async def _ocs_get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET an OCS endpoint and unwrap the ``ocs.data`` payload.

        Args:
            path: Path under ``API_BASE`` (e.g. ``/account/list``)
            params: Optional query params (``format=json`` is added automatically)

        Returns:
            The ``data`` payload (a list, dict, or string depending on endpoint)
        """
        query: dict[str, Any] = {"format": "json"}
        if params:
            query.update(params)
        response = await self._make_request(
            "GET",
            f"{self.API_BASE}{path}",
            params=query,
            headers=self._OCS_HEADERS,
        )
        body = response.json()
        # Standard OCS envelope: {"ocs": {"meta": {...}, "data": <payload>}}
        return body.get("ocs", {}).get("data")

    # --- Accounts ---

    async def list_accounts(self) -> list[dict[str, Any]]:
        """List the user's configured mail accounts.

        Returns:
            List of account objects (keys: id, email, isDelegated, aliases)
        """
        data = await self._ocs_get("/account/list")
        return data or []

    # --- Mailboxes ---

    async def get_mailboxes(self, account_id: int) -> list[dict[str, Any]]:
        """List the mailboxes (folders) of an account.

        Args:
            account_id: Account ID (the ``id`` from :meth:`list_accounts`)

        Returns:
            List of mailbox objects. Note ``databaseId`` is the numeric mailbox
            id needed by :meth:`list_messages` (``id`` is a base64 string).
        """
        data = await self._ocs_get("/mailboxes", params={"accountId": account_id})
        return data or []

    # --- Messages ---

    async def list_messages(
        self,
        mailbox_id: int,
        *,
        cursor: int | None = None,
        filter: str | None = None,
        limit: int = 20,
        view: str | None = None,
    ) -> list[dict[str, Any]]:
        """List message envelopes in a mailbox (newest first).

        Reads DB-cached envelope metadata, so this is fast and does not hit
        IMAP per request.

        Args:
            mailbox_id: Numeric mailbox id (``databaseId`` from get_mailboxes)
            cursor: Pagination cursor (timestamp/id from a prior page)
            filter: Optional search/filter query
            limit: Max messages to return. Clamped server-side to 1..100; a
                missing limit collapses to 1 server-side, so always pass one.
            view: ``"singleton"`` or ``"threaded"`` (default threaded)

        Returns:
            List of message summary objects (keys include databaseId, subject,
            from, to, dateInt, flags, previewText, mailboxId).
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if filter is not None:
            params["filter"] = filter
        if view is not None:
            params["view"] = view
        data = await self._ocs_get(f"/mailboxes/{mailbox_id}/messages", params=params)
        return data or []

    async def get_message(self, message_id: int) -> dict[str, Any]:
        """Get a single message with its full body.

        The Mail app fetches the body from IMAP server-side and returns it as a
        single ``body`` field (sanitized HTML when ``hasHtmlBody`` is true,
        otherwise plain text). ``body`` may be absent on partial (206) responses
        when S/MIME decryption fails.

        Args:
            message_id: Numeric message id (``databaseId`` from list_messages)

        Returns:
            Full message object.
        """
        data = await self._ocs_get(f"/message/{message_id}")
        return data or {}

    async def get_attachment(
        self, message_id: int, attachment_id: str
    ) -> dict[str, Any]:
        """Get a single attachment's metadata and content.

        The Mail OCS API returns the attachment as a JSON object (not a binary
        download): keys ``name``, ``mime``, ``size``, ``content``.

        Args:
            message_id: Numeric message id
            attachment_id: Attachment id (a string; from the message's
                ``attachments`` array)

        Returns:
            Attachment object with name, mime, size, content.
        """
        data = await self._ocs_get(f"/message/{message_id}/attachment/{attachment_id}")
        return data or {}
