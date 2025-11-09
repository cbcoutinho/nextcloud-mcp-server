"""Nextcloud Groups API client."""

import logging
from typing import List

from .base import BaseNextcloudClient, retry_on_429

logger = logging.getLogger(__name__)


class GroupsClient(BaseNextcloudClient):
    """Client for Nextcloud Groups API operations."""

    app_name = "groups"

    @retry_on_429
    async def search_groups(
        self,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> List[str]:
        """
        Search for groups on the Nextcloud server.

        Args:
            search: Optional search string to filter groups
            limit: Optional limit for number of results
            offset: Optional offset for pagination

        Returns:
            List of group IDs matching the search criteria
        """
        params = {}
        if search is not None:
            params["search"] = search
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        response = await self._client.get(
            "/ocs/v2.php/cloud/groups",
            params=params,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

        groups = data["ocs"]["data"].get("groups", [])
        return groups

    @retry_on_429
    async def create_group(self, groupid: str) -> None:
        """
        Create a new group.

        Args:
            groupid: The group ID to create

        Raises:
            HTTPStatusError: If the request fails (e.g., group already exists)
        """
        response = await self._client.post(
            "/ocs/v2.php/cloud/groups",
            data={"groupid": groupid},
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        logger.info(f"Created group: {groupid}")

    @retry_on_429
    async def delete_group(self, groupid: str) -> None:
        """
        Delete a group.

        Args:
            groupid: The group ID to delete

        Raises:
            HTTPStatusError: If the request fails (e.g., group doesn't exist)
        """
        response = await self._client.delete(
            f"/ocs/v2.php/cloud/groups/{groupid}",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        logger.info(f"Deleted group: {groupid}")

    @retry_on_429
    async def get_group_members(self, groupid: str) -> List[str]:
        """
        Get members of a group.

        Args:
            groupid: The group ID

        Returns:
            List of usernames in the group
        """
        response = await self._client.get(
            f"/ocs/v2.php/cloud/groups/{groupid}",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

        users = data["ocs"]["data"].get("users", [])
        return users

    @retry_on_429
    async def get_group_subadmins(self, groupid: str) -> List[str]:
        """
        Get subadmins of a group.

        Args:
            groupid: The group ID

        Returns:
            List of usernames who are subadmins of the group
        """
        response = await self._client.get(
            f"/ocs/v2.php/cloud/groups/{groupid}/subadmins",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

        # The API returns data as a list or dict depending on results
        subadmins_data = data["ocs"]["data"]
        if isinstance(subadmins_data, list):
            return subadmins_data
        return []

    @retry_on_429
    async def update_group_displayname(self, groupid: str, displayname: str) -> None:
        """
        Update a group's display name.

        Args:
            groupid: The group ID
            displayname: The new display name

        Raises:
            HTTPStatusError: If the request fails
        """
        response = await self._client.put(
            f"/ocs/v2.php/cloud/groups/{groupid}",
            data={"key": "displayname", "value": displayname},
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        response.raise_for_status()
        logger.info(f"Updated group {groupid} displayname to: {displayname}")
