"""Client for the Nextcloud Mail app API.

Uses two endpoint types:

1. **OCS routes** — ``/ocs/v2.php/apps/mail/message/...`` — for reading full messages
   (body + metadata) and the raw RFC 2822 source. Works with Basic Auth (App
   Password) via the OCS-APIRequest header. These routes are registered in the
   ``'ocs'`` section of the Mail app's ``routes.php``.

2. **Direct app routes** — ``/index.php/apps/mail/api/...`` — for listing accounts,
   mailboxes, and messages, downloading attachments, and the two-step outbox send.
   These REST resource routes (from ``'resources'`` in ``routes.php``) are
   CSRF-gated for browser sessions, but Nextcloud exempts any request carrying the
   ``OCS-APIRequest: true`` header from the CSRF check — so Basic Auth (App
   Password) + that header is sufficient. No ``requesttoken`` round-trip is needed
   (verified end-to-end against a live Mail 5.x backend; see the GreenMail
   integration tests).

Attachment downloads use the direct route ``/api/messages/{id}/attachment/{id}``,
which returns the raw file bytes (via core's ``DownloadResponse``). The OCS
``/message/{id}/attachment/{id}`` route is unreliable across Mail versions — on
some setups it returns HTTP 200 with an empty, non-JSON body (see GH #989).
"""

import base64
from email.message import Message
from typing import Any
from urllib.parse import quote

from httpx import HTTPStatusError, RequestError, Response

from nextcloud_mcp_server.client.base import BaseNextcloudClient


def _ocs_response(response: Response) -> Any:
    """Unwrap the OCS envelope from a response and validate the meta status.

    Args:
        response: The httpx response object from an OCS endpoint.

    Returns:
        The ``ocs.data`` payload.

    Raises:
        HTTPStatusError: If the OCS meta indicates failure.
        RequestError: If the response is not valid JSON.
    """
    try:
        body = response.json()
    except Exception:
        raise RequestError(
            f"Response is not valid JSON: {response.text[:200]}"
        ) from None

    if not isinstance(body, dict):
        raise RequestError(
            f"Unexpected response format: expected dict, got {type(body).__name__}"
        )

    ocs = body.get("ocs")
    if not isinstance(ocs, dict):
        raise RequestError(
            f"Unexpected OCS format: expected dict, got {type(ocs).__name__}"
        )

    meta = ocs.get("meta", {})
    if not isinstance(meta, dict):
        meta = {}
    status_code = int(meta.get("statuscode", 200) or 200)

    if status_code >= 400:
        message = meta.get("message", "Unknown OCS error")
        # Synthesise a Response-like object so callers can check status_code
        synthetic = Response(status_code=status_code, text=message)
        raise HTTPStatusError(message, request=response.request, response=synthetic)

    return ocs.get("data")


class MailClient(BaseNextcloudClient):
    """Client for the Nextcloud Mail app API.

    Uses a combination of OCS and direct routes to cover the full mail workflow
    (list accounts → browse mailboxes → list messages → read message → send
    reply) with the correct authentication for each endpoint.

    Requests go through :meth:`BaseNextcloudClient._make_request`, which routes
    bare ``/apps/...`` paths through ``_resolve_url`` (prepending ``/index.php``,
    the universal entry point that works without pretty-URL rewriting — issue
    #732), applies 429 retry, tracing, and ``raise_for_status``. ``/ocs/...``
    paths pass through ``_resolve_url`` unchanged.
    """

    # OCS endpoints — work with Basic Auth + OCS‑APIRequest header alone.
    OCS_BASE = "/ocs/v2.php/apps/mail"

    # Direct API endpoints — CSRF-exempt via the OCS-APIRequest header. Written
    # as bare ``/apps/...`` paths (``_resolve_url`` prepends ``/index.php``).
    API_BASE = "/apps/mail/api"

    # The OCS-APIRequest header authenticates the OCS routes and exempts the
    # direct routes from CSRF, so both families use the same headers.
    _API_HEADERS = {
        "OCS-APIRequest": "true",
        "Accept": "application/json",
    }

    app_name = "mail"

    # ------------------------------------------------------------------
    # Low‑level request helpers
    # ------------------------------------------------------------------

    async def _ocs_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET an OCS endpoint and unwrap its envelope.

        Args:
            path: Path relative to :attr:`OCS_BASE` (e.g. ``/message/1``).
            params: Optional query-string parameters.

        Returns:
            The ``ocs.data`` payload (list or dict).
        """
        query: dict[str, Any] = {"format": "json"}
        if params:
            query.update(params)

        response = await self._make_request(
            "GET",
            f"{self.OCS_BASE}{path}",
            params=query,
            headers=self._API_HEADERS,
        )
        # ``_make_request`` (base) already raised on any non-2xx status; an OCS
        # meta failure arrives as HTTP 200 and is handled by ``_ocs_response``.
        return _ocs_response(response)

    async def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a direct API endpoint (CSRF-exempt via the OCS-APIRequest header).

        Args:
            path: Path relative to :attr:`API_BASE` (e.g. ``/accounts``).
            params: Optional query-string parameters.

        Returns:
            The parsed JSON response body.
        """
        response = await self._make_request(
            "GET",
            f"{self.API_BASE}{path}",
            params=params,
            headers=self._API_HEADERS,
        )
        # ``_make_request`` (base) already raised on any non-2xx status.
        return response.json()

    async def _api_post(
        self, path: str, json_data: dict[str, Any] | None = None
    ) -> Any:
        """POST to a direct API endpoint (CSRF-exempt via the OCS-APIRequest header).

        Args:
            path: Path relative to :attr:`API_BASE` (e.g. ``/outbox``).
            json_data: JSON body to send.

        Returns:
            The parsed JSON response body.
        """
        response = await self._make_request(
            "POST",
            f"{self.API_BASE}{path}",
            json=json_data,
            headers={**self._API_HEADERS, "Content-Type": "application/json"},
        )
        # ``_make_request`` (base) already raised on any non-2xx status. The
        # outbox send step can legitimately return an empty body (e.g. 204);
        # don't choke trying to JSON-decode it.
        return response.json() if response.content else {}

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def list_accounts(self) -> list[dict[str, Any]]:
        """List all configured mail accounts.

        Uses ``GET /index.php/apps/mail/api/accounts`` (direct resource route).

        Returns:
            List of raw account dicts. Mail 5.x keys the address as
            ``emailAddress`` (the server layer maps it onto ``MailAccount.email``
            via the field alias).
        """
        data = await self._api_get("/accounts")
        return data if isinstance(data, list) else []

    async def get_mailboxes(self, account_id: int) -> list[dict[str, Any]]:
        """List mailboxes (folders) for a given account.

        Uses ``GET /index.php/apps/mail/api/mailboxes?accountId=X``.

        Mail 5.x wraps the folders in an account envelope —
        ``{"id", "email", "mailboxes": [...], "delimiter"}`` — so the list is
        nested under ``mailboxes`` rather than returned at the top level.

        Args:
            account_id: The account ID.

        Returns:
            List of mailbox dicts.
        """
        data = await self._api_get("/mailboxes", params={"accountId": account_id})
        if isinstance(data, dict):
            mailboxes = data.get("mailboxes", [])
            return mailboxes if isinstance(mailboxes, list) else []
        # Defensive: tolerate a bare list if a future/older version returns one.
        return data if isinstance(data, list) else []

    async def list_messages(
        self,
        mailbox_id: int,
        *,
        cursor: int | None = None,
        search_filter: str | None = None,
        limit: int = 20,
        view: str | None = None,
    ) -> list[dict[str, Any]]:
        """List messages in a mailbox.

        Uses ``GET /index.php/apps/mail/api/messages?mailboxId=X``.

        Args:
            mailbox_id: The mailbox database ID.
            cursor: Pagination cursor (the ``databaseId`` of the last message
                    from the previous page).
            search_filter: Optional search/filter string.
            limit: Max messages to return (clamped to 1‑100).
            view: ``"singleton"`` or ``"threaded"`` (default is threaded).

        Returns:
            List of message summary dicts.
        """
        params: dict[str, Any] = {
            "mailboxId": mailbox_id,
            "limit": min(max(1, limit), 100),
        }
        if cursor is not None:
            params["cursor"] = cursor
        if search_filter is not None:
            params["filter"] = search_filter
        if view is not None:
            params["view"] = view

        data = await self._api_get("/messages", params=params)
        return data if isinstance(data, list) else []

    async def get_message(self, message_id: int) -> dict[str, Any]:
        """Get a single message with full body content and metadata.

        Uses the OCS route ``GET /ocs/v2.php/apps/mail/message/{id}``
        which works with Basic Auth (App Password).

        Args:
            message_id: The message database ID.

        Returns:
            Full message dict with ``body``, ``attachments``, ``from``, ``to``,
            ``subject``, ``flags``, etc.
        """
        data = await self._ocs_get(f"/message/{message_id}")
        return data if isinstance(data, dict) else {}

    async def get_attachment(
        self, message_id: int, attachment_id: str
    ) -> dict[str, Any]:
        """Get an attachment's content from a message.

        Uses the direct route
        ``GET /index.php/apps/mail/api/messages/{id}/attachment/{id}``
        (CSRF-exempt via the OCS-APIRequest header), which returns the raw file
        bytes. The OCS ``/message/{id}/attachment/{id}`` route is unreliable
        across Mail versions — on some setups it returns HTTP 200 with an empty,
        non-JSON body (GH #989).

        The ``attachment_id`` is URL‑encoded to prevent path traversal.

        Args:
            message_id: The message database ID.
            attachment_id: The attachment ID string.

        Returns:
            Attachment dict with keys ``name``, ``mime``, ``size``, ``content``.
            ``content`` is the raw attachment bytes base64-encoded.
        """
        safe_id = quote(attachment_id, safe="")
        # Binary download: unlike the JSON helpers, don't send
        # ``Accept: application/json`` (mirrors ``DeckClient.get_attachment_file``).
        # The ``OCS-APIRequest`` header still CSRF-exempts this direct route.
        response = await self._make_request(
            "GET",
            f"{self.API_BASE}/messages/{message_id}/attachment/{safe_id}",
            headers={"OCS-APIRequest": "true"},
        )
        # ``_make_request`` (base) already raised on any non-2xx status.
        raw = response.content
        ctype = (response.headers.get("content-type", "") or "").split(";")[0].strip()

        # Recover the real filename from the Content-Disposition header (core's
        # DownloadResponse sets ``attachment; filename="..."``); fall back to a
        # synthetic name when the header is absent or has no filename.
        name = (
            self._filename_from_disposition(response.headers.get("content-disposition"))
            or f"attachment_{message_id}_{attachment_id}"
        )

        return {
            "name": name,
            "mime": ctype or "application/octet-stream",
            "size": len(raw),
            "content": base64.b64encode(raw).decode("ascii"),
        }

    @staticmethod
    def _filename_from_disposition(header: str | None) -> str | None:
        """Extract the filename from a Content-Disposition header, if present.

        Uses ``email.message.Message`` so quoted and RFC 2231-encoded filenames
        are handled without the deprecated ``cgi`` module. Returns ``None`` when
        the header is missing or carries no filename.
        """
        if not header:
            return None
        msg = Message()
        msg["content-disposition"] = header
        return msg.get_filename()

    async def send_message(
        self,
        account_id: int,
        to: list[dict[str, str]],
        subject: str,
        body: str,
        is_html: bool = False,
        cc: list[dict[str, str]] | None = None,
        bcc: list[dict[str, str]] | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        """Send an email via the Mail 5.x outbox API.

        Mail 5.x uses a two-step outbox flow (both CSRF-exempt via the
        OCS-APIRequest header):
        1. POST ``/api/outbox`` to stage the message
        2. POST ``/api/outbox/{id}`` to send it

        The ``From:`` identity is derived by the Mail app from ``account_id``
        (``OutboxController::create`` has no from/email field), so it is not
        sent here.

        Args:
            account_id: The mail account ID to send from.
            to: List of recipients ``[{"label": "...", "email": "..."}]``.
            subject: Subject line.
            body: Message body (plain text or HTML depending on ``is_html``).
            is_html: Whether ``body`` contains HTML.
            cc: Optional CC recipients (same format as ``to``).
            bcc: Optional BCC recipients (same format as ``to``).
            references: Optional RFC 2822 ``Message-ID`` for reply threading.

        Returns:
            The response dict from the send step.
        """
        create_data: dict[str, Any] = {
            "accountId": account_id,
            "subject": subject,
            "isHtml": is_html,
            "smimeSign": False,
            "smimeEncrypt": False,
            "to": to,
        }
        if is_html:
            create_data["bodyHtml"] = body
        else:
            create_data["bodyPlain"] = body
        if cc:
            create_data["cc"] = cc
        if bcc:
            create_data["bcc"] = bcc
        if references:
            create_data["inReplyToMessageId"] = references

        create_result = await self._api_post("/outbox", json_data=create_data)
        outbox_id = create_result.get("data", {}).get("id")
        if not outbox_id:
            # Raise (don't return an error dict) so the server tool's
            # ``except RequestError`` surfaces it — otherwise the discarded
            # return value would let the tool report success on a covert failure.
            raise RequestError(
                f"Outbox create returned no id; response: {create_result!r}"
            )

        send_result = await self._api_post(f"/outbox/{outbox_id}", json_data={})

        return {
            "success": True,
            "message": "Message sent successfully",
            "outbox_id": outbox_id,
            "response": send_result,
        }

    async def get_message_raw(self, message_id: int) -> str | None:
        """Get the raw RFC 2822 source of a message.

        Uses the OCS route ``GET /ocs/v2.php/apps/mail/message/{id}/raw``.

        Args:
            message_id: The message database ID.

        Returns:
            The raw email source string, or ``None`` if not found.
        """
        data = await self._ocs_get(f"/message/{message_id}/raw")
        return data if isinstance(data, str) else None
