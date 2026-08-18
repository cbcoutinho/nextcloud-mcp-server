"""Nextcloud OCS Sharing API client for file/folder sharing operations."""

import logging
from typing import Any

from nextcloud_mcp_server.models.sharing import ShareType

from .base import BaseNextcloudClient, retry_on_429
from .ocs import OCS_REQUEST_HEADERS, describe_ocs_failure, parse_ocs_envelope

logger = logging.getLogger(__name__)


class PublicLinkRecipientError(ValueError):
    """A public-link share was given a recipient it cannot address.

    Its own type, rather than a plain ``ValueError``, so callers can attach a
    redirect that only fits this case without matching on message text.
    """


def validate_share_with(share_type: int, share_with: str | None) -> None:
    """Check the ``shareType``/``shareWith`` pairing before it reaches the wire.

    The case worth guarding is a public link that carries a recipient.
    Nextcloud does not reject it: it ignores ``shareWith`` and returns a
    perfectly valid anonymous link. The caller is then told the share
    succeeded and reasonably believes the file went to the named person, when
    it was actually published to anyone holding the URL. A silent success with
    the wrong audience is worse than an error, which is why this is checked
    client-side rather than left to the server.

    The inverse — a recipient-typed share with no ``shareWith`` — does fail
    server-side, but as a generic OCS 400 that names neither the field nor what
    belongs in it.

    Args:
        share_type: OCS ``shareType`` value (see :class:`ShareType`).
        share_with: Recipient identifier, if any.

    Raises:
        PublicLinkRecipientError: If a public link carries a recipient.
        ValueError: If a recipient-typed share is missing one.
    """
    has_recipient = bool(share_with and share_with.strip())

    if share_type == ShareType.PUBLIC_LINK:
        if has_recipient:
            # Deliberately names no alternative *call* here. This message is
            # shared between direct client callers and the MCP tool, which
            # surfaces it verbatim, and the two layers have different names for
            # the same operation -- pointing an agent at a callable that does
            # not exist on its side is worse than not suggesting one. The tool
            # appends its own suggestion when it translates this.
            raise PublicLinkRecipientError(
                f"shareType {ShareType.PUBLIC_LINK} (public link) must not carry "
                f"shareWith: Nextcloud ignores the recipient and publishes the "
                f"file to anyone holding the URL, so it would NOT be shared with "
                f"{share_with!r}. Use shareType {ShareType.USER} (user) or "
                f"{ShareType.GROUP} (group) to share with a recipient, or omit "
                f"shareWith to create an anonymous public link."
            )
        return

    # Everything that is not a public link addresses someone. Unknown types are
    # treated as recipient-typed rather than rejected outright -- Nextcloud may
    # add share types we do not know about, and refusing them here would break
    # a caller that is otherwise correct.
    if not has_recipient:
        raise ValueError(
            f"shareType {share_type} requires a non-empty shareWith recipient "
            "(user id, group id, email address, federated user@remote, circle "
            "id, Talk conversation token or Deck card id, depending on the type)"
        )


def _ocs_data(payload: Any) -> Any:
    """Validate an OCS envelope and return its ``data``.

    Raises ``RuntimeError`` -- the type this client has always raised and the
    one its tests assert on. The envelope parsing and the failure wording come
    from :mod:`.ocs` so every OCS client says the same thing about a given
    status code, 997 in particular.
    """
    envelope = parse_ocs_envelope(payload)
    if not envelope.is_success:
        raise RuntimeError(describe_ocs_failure(envelope.status_code, envelope.message))
    return envelope.data


class SharingClient(BaseNextcloudClient):
    """Client for Nextcloud OCS Sharing API operations."""

    app_name = "sharing"

    @retry_on_429
    async def create_share(
        self,
        path: str,
        share_with: str | None = None,
        share_type: int = 0,
        permissions: int = 1,
    ) -> dict[str, Any]:
        """Create a share for a file or folder.

        Args:
            path: Path to file/folder to share (relative to user's files)
            share_with: Recipient identifier — user id, group id, email address,
                federated ``user@remote``, circle id, Talk conversation token or
                Deck card id, depending on ``share_type``. Omit it only for a
                public link (``share_type=3``), which addresses nobody.
            share_type: OCS share type — see :class:`ShareType`. 0=user
                (default), 1=group, 3=public link, 4=email, 6=federated,
                7=circle, 10=Talk conversation, 12=Deck card
            permissions: Share permissions:
                - 1 = read
                - 2 = update
                - 4 = create
                - 8 = delete
                - 16 = share
                - 31 = all permissions
                Common combinations: 1 (read-only), 3 (read+update), 15 (read+update+create+delete)

        Returns:
            Share data including share ID

        Raises:
            PublicLinkRecipientError: If a public link carries a recipient. A
                ``ValueError`` subclass, so existing ``except ValueError``
                handlers keep working; catch it specifically to react to just
                that case.
            ValueError: If a recipient-typed share is missing its recipient.
            HTTPStatusError: If the request fails
        """
        validate_share_with(share_type, share_with)

        payload: dict[str, Any] = {
            "path": path,
            "shareType": share_type,
            "permissions": permissions,
        }
        # Omit shareWith entirely for a public link rather than sending an empty
        # value: validate_share_with has already established there is no
        # recipient, and OCS treats a present-but-empty field inconsistently.
        # Send it trimmed, matching the value validation just accepted -- an
        # untrimmed " alice " would otherwise reach OCS as a different recipient
        # id than the one that was checked.
        if share_with and share_with.strip():
            payload["shareWith"] = share_with.strip()

        response = await self._client.post(
            "/ocs/v2.php/apps/files_sharing/api/v1/shares",
            headers=OCS_REQUEST_HEADERS,
            data=payload,
        )
        response.raise_for_status()
        data = response.json()

        share_data = _ocs_data(data)

        # An OK status with no data still means the share was not created.
        if not share_data:
            envelope = parse_ocs_envelope(data)
            raise RuntimeError(
                f"Share creation failed: {envelope.message} "
                f"(status {envelope.status_code})"
            )

        logger.info(
            "Created share %s: %s -> %s (type=%s, permissions=%s)",
            share_data["id"],
            path,
            share_with,
            share_type,
            permissions,
        )
        return share_data

    @retry_on_429
    async def create_public_link(
        self,
        path: str,
        permissions: int = 1,
        expire_date: str | None = None,
    ) -> dict[str, Any]:
        """Create a public link share (``shareType=3``) for a file or folder.

        Unlike :meth:`create_share`, this targets anonymous public access, so no
        ``shareWith`` recipient is sent. The returned data carries the public
        ``url`` and ``token`` for the link.

        Args:
            path: Path to file/folder to share (relative to the user's files)
            permissions: Share permissions (default: 1 = read-only). See
                :meth:`create_share` for the bit values.
            expire_date: Optional expiry as ``YYYY-MM-DD``. Nextcloud enforces
                public-link expiry at date granularity — the link expires at
                midnight (start of this date) in the owner's timezone.

        Returns:
            Share data including the public ``url`` and ``token``

        Raises:
            HTTPStatusError: If the request fails
            RuntimeError: If the OCS API reports an error
        """
        data: dict[str, Any] = {
            "path": path,
            "shareType": ShareType.PUBLIC_LINK,
            "permissions": permissions,
        }
        if expire_date is not None:
            data["expireDate"] = expire_date

        response = await self._client.post(
            "/ocs/v2.php/apps/files_sharing/api/v1/shares",
            headers=OCS_REQUEST_HEADERS,
            data=data,
        )
        response.raise_for_status()
        result = response.json()

        share_data = _ocs_data(result)

        # An OK status with no data still means the link was not created.
        if not share_data:
            envelope = parse_ocs_envelope(result)
            raise RuntimeError(
                f"Public link creation failed: {envelope.message} "
                f"(status {envelope.status_code})"
            )

        logger.info(
            "Created public link %s: %s (permissions=%s, expire_date=%s)",
            share_data["id"],
            path,
            permissions,
            expire_date,
        )
        return share_data

    @retry_on_429
    async def delete_share(self, share_id: int) -> None:
        """Delete a share by its ID.

        Args:
            share_id: The share ID to delete

        Raises:
            HTTPStatusError: If the request fails
        """
        response = await self._client.delete(
            f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}",
            headers=OCS_REQUEST_HEADERS,
        )
        response.raise_for_status()
        data = response.json()

        _ocs_data(data)

        logger.info("Deleted share %s", share_id)

    @retry_on_429
    async def get_share(self, share_id: int) -> dict[str, Any]:
        """Get information about a specific share.

        Args:
            share_id: The share ID

        Returns:
            Share data

        Raises:
            HTTPStatusError: If the request fails
        """
        response = await self._client.get(
            f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}",
            headers=OCS_REQUEST_HEADERS,
        )
        response.raise_for_status()
        data = response.json()

        _ocs_data(data)

        share_data = data["ocs"]["data"]
        # The API returns a list with a single share, extract the first element
        if isinstance(share_data, list) and len(share_data) > 0:
            return share_data[0]
        return share_data

    @retry_on_429
    async def list_shares(
        self, path: str | None = None, shared_with_me: bool = False
    ) -> list[dict[str, Any]]:
        """List shares.

        Args:
            path: Optional path to filter shares for a specific file/folder
            shared_with_me: If True, list shares shared with the current user

        Returns:
            List of share data

        Raises:
            HTTPStatusError: If the request fails
        """
        params = {}
        if path:
            params["path"] = path
        if shared_with_me:
            params["shared_with_me"] = "true"

        response = await self._client.get(
            "/ocs/v2.php/apps/files_sharing/api/v1/shares",
            params=params,
            headers=OCS_REQUEST_HEADERS,
        )
        response.raise_for_status()
        data = response.json()

        _ocs_data(data)

        # Handle both single share and list of shares
        shares_data = data["ocs"]["data"]
        if isinstance(shares_data, dict):
            return [shares_data]
        return shares_data if shares_data else []

    @retry_on_429
    async def update_share(
        self, share_id: int, permissions: int | None = None
    ) -> dict[str, Any]:
        """Update a share's permissions.

        Args:
            share_id: The share ID to update
            permissions: New permissions value (see create_share for values)

        Returns:
            Updated share data

        Raises:
            HTTPStatusError: If the request fails
        """
        data = {}
        if permissions is not None:
            data["permissions"] = permissions

        response = await self._client.put(
            f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}",
            headers=OCS_REQUEST_HEADERS,
            data=data,
        )
        response.raise_for_status()
        result = response.json()

        _ocs_data(result)

        logger.info("Updated share %s", share_id)
        return result["ocs"]["data"]
