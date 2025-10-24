"""MCP tools for Nextcloud file/folder sharing operations."""

import json

from mcp.server.fastmcp import Context, FastMCP

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client


def configure_sharing_tools(mcp: FastMCP):
    """Configure sharing-related MCP tools.

    Args:
        mcp: FastMCP server instance
    """

    @mcp.tool()
    @require_scopes("sharing:write")
    async def nc_share_create(
        path: str,
        share_with: str,
        ctx: Context,
        share_type: int = 0,
        permissions: int = 1,
    ) -> str:
        """Create a share for a file or folder in Nextcloud.

        Share a file or folder with another user or group. The authenticated user
        must own the file/folder being shared.

        Args:
            path: Path to file/folder to share (relative to your files, e.g., "/document.txt")
            share_with: Username (for user share) or group name (for group share)
            share_type: Share type - 0 for user (default), 1 for group, 3 for public link
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
        """
        client = get_client(ctx)
        share_data = await client.sharing.create_share(
            path=path,
            share_with=share_with,
            share_type=share_type,
            permissions=permissions,
        )
        return json.dumps(share_data, indent=2)

    @mcp.tool()
    @require_scopes("sharing:write")
    async def nc_share_delete(share_id: int, ctx: Context) -> str:
        """Delete a share by its ID.

        Remove a share that you created. You must be the owner of the share.

        Args:
            share_id: The ID of the share to delete

        Returns:
            JSON string confirming deletion
        """
        client = get_client(ctx)
        await client.sharing.delete_share(share_id)
        return json.dumps(
            {"success": True, "message": f"Share {share_id} deleted"}, indent=2
        )

    @mcp.tool()
    @require_scopes("sharing:write")
    async def nc_share_get(share_id: int, ctx: Context) -> str:
        """Get information about a specific share.

        Retrieve details about a share by its ID. You must have access to the share
        (either as owner or recipient).

        Args:
            share_id: The ID of the share

        Returns:
            JSON string with share information
        """
        client = get_client(ctx)
        share_data = await client.sharing.get_share(share_id)
        return json.dumps(share_data, indent=2)

    @mcp.tool()
    @require_scopes("sharing:write")
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
        client = get_client(ctx)
        shares = await client.sharing.list_shares(
            path=path, shared_with_me=shared_with_me
        )
        return json.dumps(shares, indent=2)

    @mcp.tool()
    @require_scopes("sharing:write")
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
        client = get_client(ctx)
        share_data = await client.sharing.update_share(
            share_id=share_id, permissions=permissions
        )
        return json.dumps(share_data, indent=2)
