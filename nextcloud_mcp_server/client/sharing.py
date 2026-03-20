"""Nextcloud OCS Sharing API client for file/folder sharing operations."""

import logging
from typing import Any
from urllib.parse import unquote

from .base import BaseNextcloudClient, retry_on_429

logger = logging.getLogger(__name__)


class SharingClient(BaseNextcloudClient):
    """Client for Nextcloud OCS Sharing API operations."""

    app_name = "sharing"

    def __init__(self, http_client, username: str):
        super().__init__(http_client, username)
        self._resolved_user_ids: dict[str, str] = {}

    async def _resolve_user_id(self, share_with: str) -> str:
        """Resolve Nextcloud internal user id (UUID) for `share_type=0`.

        On some Nextcloud instances `files_sharing` expects `shareWith` to be
        an internal user id rather than the login/username.
        """
        # Already looks like a UUID/principal id.
        if share_with in self._resolved_user_ids:
            return self._resolved_user_ids[share_with]

        try:
            resp = await self._client.get(
                "/ocs/v2.php/cloud/users",
                params={"search": share_with},
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            users = data.get("ocs", {}).get("data", {}).get("users") or []
            if users:
                self._resolved_user_ids[share_with] = users[0]
                return users[0]
        except Exception as e:
            logger.debug(
                "Failed to resolve internal user id for '%s': %s",
                share_with,
                e,
            )

        return share_with

    @retry_on_429
    async def create_share(
        self,
        path: str,
        share_with: str,
        share_type: int = 0,
        permissions: int = 1,
    ) -> dict[str, Any]:
        """Create a share for a file or folder.

        Args:
            path: Path to file/folder to share (relative to user's files)
            share_with: Username (for user share) or group name (for group share)
            share_type: Share type (0=user, 1=group, 3=public link)
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
            HTTPStatusError: If the request fails
        """
        # Nextcloud can provide the "path" in multiple forms:
        # - decoded relative file path (what OCS expects)
        # - percent-encoded path (from WebDAV href segments)
        # - WebDAV absolute-like href: /remote.php/dav/files/<principal>/<rel-path>
        #
        # Normalize to "relative to user's files root" before calling OCS.
        path = unquote(path)

        # Strip WebDAV DAV prefix if a href was passed by caller/agent.
        # Keep everything after /remote.php/dav/files/<principal>/
        dav_prefix = "/remote.php/dav/files/"
        if dav_prefix in path:
            after = path.split(dav_prefix, 1)[1]
            # after: "<principal>/<rest>"
            parts = after.split("/", 1)
            if len(parts) == 2:
                path = parts[1]
            else:
                path = ""

        # OCS expects path without leading slash.
        path = path.lstrip("/")

        # For user shares Nextcloud may expect internal UUID user id.
        if share_type == 0 and share_with:
            share_with = await self._resolve_user_id(share_with)

        response = await self._client.post(
            "/ocs/v2.php/apps/files_sharing/api/v1/shares",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            data={
                "path": path,
                "shareType": share_type,
                "shareWith": share_with,
                "permissions": permissions,
            },
        )
        response.raise_for_status()
        data = response.json()

        # OCS API v2 uses HTTP-style status codes (200 for success)
        # OCS API v1 used custom codes (100 for success)
        ocs_status = data["ocs"]["meta"]["statuscode"]
        if ocs_status not in (100, 200):
            ocs_message = data["ocs"]["meta"].get("message", "Unknown error")
            raise RuntimeError(f"OCS API error (code {ocs_status}): {ocs_message}")

        share_data = data["ocs"]["data"]

        # Handle case where data might be an empty list on error
        if not share_data or (isinstance(share_data, list) and len(share_data) == 0):
            ocs_message = data["ocs"]["meta"].get("message", "Unknown error")
            raise RuntimeError(
                f"Share creation failed: {ocs_message} (status {ocs_status})"
            )

        logger.info(
            f"Created share {share_data['id']}: {path} -> {share_with} "
            f"(type={share_type}, permissions={permissions})"
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
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

        if data["ocs"]["meta"]["statuscode"] not in (100, 200):
            raise RuntimeError(
                f"OCS API error: {data['ocs']['meta'].get('message', 'Unknown error')}"
            )

        logger.info(f"Deleted share {share_id}")

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
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

        if data["ocs"]["meta"]["statuscode"] not in (100, 200):
            raise RuntimeError(
                f"OCS API error: {data['ocs']['meta'].get('message', 'Unknown error')}"
            )

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
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

        if data["ocs"]["meta"]["statuscode"] not in (100, 200):
            raise RuntimeError(
                f"OCS API error: {data['ocs']['meta'].get('message', 'Unknown error')}"
            )

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
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            data=data,
        )
        response.raise_for_status()
        result = response.json()

        if result["ocs"]["meta"]["statuscode"] not in (100, 200):
            raise RuntimeError(
                f"OCS API error: {result['ocs']['meta'].get('message', 'Unknown error')}"
            )

        logger.info(f"Updated share {share_id}")
        return result["ocs"]["data"]
