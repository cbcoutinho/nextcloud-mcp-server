"""MCP tools for Nextcloud file/folder sharing operations."""

from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.client.sharing import PublicLinkRecipientError
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models import PublicDownloadLinkResponse
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.serialization import compact_json_dumps


def _compute_link_expiry(expires_in_minutes: int, now: datetime) -> tuple[str, str]:
    """Compute ``(expire_date, expires_at)`` for a short-lived public link.

    Nextcloud expires a public link at 00:00:00 on ``expireDate`` in the owner's
    timezone (equivalently, the end of the day *before* ``expireDate``). Rounding
    ``expireDate`` up one day therefore keeps the link valid through the end of
    the target's day, guaranteeing it outlives the requested window for any
    realistic server timezone.

    Args:
        expires_in_minutes: Requested lifetime in minutes; must be positive.
        now: Current time (timezone-aware); injected for deterministic tests.

    Returns:
        A tuple of ``(expireDate as YYYY-MM-DD, expires_at as RFC3339 'Z')``.

    Raises:
        ValueError: If ``expires_in_minutes`` is not positive — this tool only
            creates short-lived links, never a permanent (non-expiring) one.
    """
    if expires_in_minutes <= 0:
        raise ToolError("expires_in_minutes must be a positive number of minutes")
    target = now + timedelta(minutes=expires_in_minutes)
    expire_date = (target.date() + timedelta(days=1)).isoformat()
    # Match BaseResponse.serialize_timestamp: only collapse a *trailing* UTC
    # offset to "Z", rather than replacing any "+00:00" occurrence.
    iso = target.isoformat()
    expires_at = iso[:-6] + "Z" if iso.endswith("+00:00") else iso
    return expire_date, expires_at


def _build_link_response(
    path: str, share_data: dict[str, Any], expires_at: str
) -> PublicDownloadLinkResponse:
    """Assemble the tool response from a raw OCS public-link share payload.

    Args:
        path: The shared file path (echoed back to the caller).
        share_data: Raw ``ocs.data`` dict from ``create_public_link``.
        expires_at: Advisory RFC3339 expiry computed by ``_compute_link_expiry``.

    Raises:
        RuntimeError: If the payload carries no ``url``. OCS always returns one
            for ``shareType=3``; its absence means the response shape changed,
            and a hard error beats handing the caller unusable empty URLs.
    """
    url = share_data.get("url", "")
    if not url:
        raise RuntimeError(
            f"Public link share {share_data.get('id')} for {path} returned no "
            "url — unexpected OCS response shape"
        )
    return PublicDownloadLinkResponse(
        path=path,
        share_id=int(share_data["id"]),
        url=url,
        # rstrip guards against a double slash if the url ever ends in "/".
        download_url=f"{url.rstrip('/')}/download",
        token=share_data.get("token"),
        permissions=int(share_data.get("permissions", 1)),
        expires_at=expires_at,
    )


def configure_sharing_tools(mcp: MCPServer):
    """Configure sharing-related MCP tools.

    Args:
        mcp: MCPServer server instance
    """

    @mcp.tool(
        title="Create Share",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("sharing.write")
    @instrument_tool
    async def nc_share_create(
        path: str,
        ctx: Context,
        share_with: str | None = None,
        share_type: int = 0,
        permissions: int = 1,
    ) -> str:
        """Create a share for a file or folder in Nextcloud.

        Share a file or folder with another user or group. The authenticated user
        must own the file/folder being shared.

        Args:
            path: Path to file/folder to share (relative to your files, e.g., "/document.txt")
            share_with: Recipient identifier, required for every share type
                except a public link: user id, group id, email address,
                federated "user@remote", circle id, Talk conversation token or
                Deck card id, matching share_type. Passing a recipient together
                with share_type=3 is rejected -- a public link addresses nobody,
                and Nextcloud would silently ignore the recipient and publish
                the file to anyone holding the URL.
            share_type: Share type - 0 for user (default), 1 for group, 4 for
                email, 6 for federated, 7 for circle, 10 for Talk conversation,
                12 for Deck card. Use 3 for a public link, though
                nc_share_create_public_link is preferred for short-lived,
                read-only download links with managed expiry
            permissions: Share permissions (default: 1 for read-only):
                - 1 = read
                - 2 = update
                - 4 = create
                - 8 = delete
                - 16 = share
                - 31 = all permissions
                Common: 1 (read-only), 3 (read+update), 15 (read+update+create+delete)

        Returns:
            JSON string with share information including share ID

        Raises:
            ToolError: If the share_type/share_with pairing is invalid
        """
        client = await get_client(ctx)
        try:
            share_data = await client.sharing.create_share(
                path=path,
                share_with=share_with,
                share_type=share_type,
                permissions=permissions,
            )
        except PublicLinkRecipientError as e:
            # The redirect fits only this rejection. The client layer names no
            # tool (its own method names do not exist on the MCP side), and this
            # is the layer that knows what the caller can actually invoke.
            raise ToolError(
                f"{e} For a public download link, use nc_share_create_public_link."
            ) from e
        except ValueError as e:
            # Every other pairing rejection -- a recipient-typed share with no
            # recipient. Suggesting the public-link tool here would be a wrong
            # redirect, which is the failure this whole message path exists to
            # avoid.
            raise ToolError(str(e)) from e
        return compact_json_dumps(share_data)

    @mcp.tool(
        title="Create Public Download Link",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("sharing.write")
    @instrument_tool
    async def nc_share_create_public_link(
        path: str,
        ctx: Context,
        expires_in_minutes: int = 30,
    ) -> PublicDownloadLinkResponse:
        """Create a short-lived, read-only public download link for a file.

        Use this for binary files (especially images) the client needs as bytes
        rather than as text: ``nc_webdav_read_file`` base64s anything it cannot
        extract text from, which can exceed the MCP client response budget and get
        truncated, leaving the file undecodable. (For documents -- PDF, DOCX --
        prefer reading them: that tool returns their text or markdown.)
        A public link keeps the MCP response small and lets the client download
        the exact original bytes from ``download_url``.

        The link is always **read-only** (``permissions=1``): a public,
        anonymously-accessible link with write access would be a security
        footgun, so it is intentionally not configurable. Use ``nc_share_create``
        (shareType=3) if you genuinely need a writable public link.

        Args:
            path: Path to the file to share (relative to your files, e.g.
                "/Receipts/receipt.jpg")
            expires_in_minutes: How long the link should remain valid, in
                minutes (default: 30). Must be positive — this tool only
                creates short-lived links, never a permanent one. See the
                expiry caveat below.

        Expiry caveat:
            Nextcloud enforces public-link expiry at **date granularity**, not
            minute precision: a link expires at 00:00:00 on ``expireDate`` in
            the owner's timezone (i.e. the end of the day before ``expireDate``).
            The requested window is rounded up so the link stays valid at least
            that long. ``expires_at`` reports the precise requested instant, but
            the link may remain valid until the end of that day server-side. To
            revoke earlier, call ``nc_share_delete`` with the returned
            ``share_id``.

        Returns:
            PublicDownloadLinkResponse with the share URL, download URL, and
            advisory expiry.

        Raises:
            ValueError: If ``expires_in_minutes`` is not positive.
        """
        expire_date, expires_at = _compute_link_expiry(
            expires_in_minutes, datetime.now(timezone.utc)
        )

        client = await get_client(ctx)
        share_data = await client.sharing.create_public_link(
            path=path,
            permissions=1,
            expire_date=expire_date,
        )
        return _build_link_response(path, share_data, expires_at)

    @mcp.tool(
        title="Delete Share",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("sharing.write")
    @instrument_tool
    async def nc_share_delete(share_id: int, ctx: Context) -> str:
        """Delete a share by its ID.

        Remove a share that you created. You must be the owner of the share.

        Args:
            share_id: The ID of the share to delete

        Returns:
            JSON string confirming deletion
        """
        client = await get_client(ctx)
        await client.sharing.delete_share(share_id)
        return compact_json_dumps(
            {"success": True, "message": f"Share {share_id} deleted"}
        )

    @mcp.tool(
        title="Get Share Details",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("sharing.write")
    @instrument_tool
    async def nc_share_get(share_id: int, ctx: Context) -> str:
        """Get information about a specific share.

        Retrieve details about a share by its ID. You must have access to the share
        (either as owner or recipient).

        Args:
            share_id: The ID of the share

        Returns:
            JSON string with share information
        """
        client = await get_client(ctx)
        share_data = await client.sharing.get_share(share_id)
        return compact_json_dumps(share_data)

    @mcp.tool(
        title="List Shares",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("sharing.write")
    @instrument_tool
    async def nc_share_list(
        ctx: Context, path: str | None = None, shared_with_me: bool = False
    ) -> str:
        """List shares created by you or shared with you.

        Args:
            path: Optional path to filter shares for a specific file/folder
            shared_with_me: If True, list shares that others shared with you.
                          If False (default), list shares you created.

        Returns:
            JSON string with list of shares
        """
        client = await get_client(ctx)
        shares = await client.sharing.list_shares(
            path=path, shared_with_me=shared_with_me
        )
        return compact_json_dumps(shares)

    @mcp.tool(
        title="Update Share",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("sharing.write")
    @instrument_tool
    async def nc_share_update(share_id: int, permissions: int, ctx: Context) -> str:
        """Update the permissions of an existing share.

        Modify the permissions for a share you created. You must be the owner.

        Args:
            share_id: The ID of the share to update
            permissions: New permissions value:
                - 1 = read
                - 2 = update
                - 4 = create
                - 8 = delete
                - 16 = share
                - 31 = all permissions
                Common: 1 (read-only), 3 (read+update), 15 (read+update+create+delete)

        Returns:
            JSON string with updated share information
        """
        client = await get_client(ctx)
        share_data = await client.sharing.update_share(
            share_id=share_id, permissions=permissions
        )
        return compact_json_dumps(share_data)
