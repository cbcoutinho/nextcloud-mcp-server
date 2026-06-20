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

from httpx import HTTPStatusError, RequestError, Response

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

        # The Mail app being absent (or a misconfigured proxy) can return HTTP
        # 200 with an HTML body; surface that as a network-style error rather
        # than letting json() raise an opaque JSONDecodeError to the caller.
        try:
            body = response.json()
        except ValueError as e:
            raise RequestError(
                f"Mail OCS returned a non-JSON response for {path}: {e}",
                request=response.request,
            ) from e

        # Standard OCS envelope: {"ocs": {"meta": {...}, "data": <payload>}}.
        # OCS can return HTTP 200 while signalling failure (e.g. 403/404) in
        # ocs.meta.statuscode; re-raise those as an HTTPStatusError carrying the
        # OCS code so callers' existing 404/403 handling applies uniformly
        # instead of silently unwrapping data=null.
        ocs = body.get("ocs", {}) if isinstance(body, dict) else {}
        meta = ocs.get("meta", {})
        # statuscode is spec'd as an int, but harden against a non-numeric value
        # in a non-spec response rather than letting int() raise ValueError
        # (which neither MCP-tool handler catches) — treat it as success.
        try:
            status_code = int(meta.get("statuscode", 200) or 200)
        except (TypeError, ValueError):
            status_code = 200
        if status_code >= 400:
            synthetic = Response(status_code=status_code, request=response.request)
            raise HTTPStatusError(
                f"Mail OCS error {status_code} for {path}: {meta.get('message')}",
                request=response.request,
                response=synthetic,
            )
        return ocs.get("data")

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
        search_filter: str | None = None,
        limit: int = 20,
        view: str | None = None,
    ) -> list[dict[str, Any]]:
        """List message envelopes in a mailbox (newest first).

        Reads DB-cached envelope metadata, so this is fast and does not hit
        IMAP per request.

        Args:
            mailbox_id: Numeric mailbox id (``databaseId`` from get_mailboxes)
            cursor: Pagination cursor (timestamp/id from a prior page)
            search_filter: Optional search/filter query (maps to the OCS
                ``filter`` query param; named to avoid shadowing ``builtins.filter``)
            limit: Max messages to return. Clamped server-side to 1..100; a
                missing limit collapses to 1 server-side, so always pass one.
            view: ``"singleton"`` or ``"threaded"`` (default threaded)

        Returns:
            List of message summary objects (keys include databaseId, subject,
            from, to, dateInt, flags, previewText, mailboxId).
        """
        # The Mail OCS API clamps limit to 1..100 server-side; do it here too so
        # the contract is enforced at the client layer with a predictable value
        # (a limit of 0 would otherwise collapse to 1 server-side).
        params: dict[str, Any] = {"limit": min(max(1, limit), 100)}
        if cursor is not None:
            params["cursor"] = cursor
        if search_filter is not None:
            params["filter"] = search_filter
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
