from typing import Dict, List, Optional

from nextcloud_mcp_server.client.base import BaseNextcloudClient
from nextcloud_mcp_server.models.users import UserDetails


class UsersClient(BaseNextcloudClient):
    """Client for Nextcloud User API operations."""

    def _get_user_headers(
        self, additional_headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """Get standard headers required for User API calls."""
        headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
        if additional_headers:
            headers.update(additional_headers)
        return headers

    async def create_user(
        self,
        userid: str,
        password: Optional[str] = None,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        groups: Optional[List[str]] = None,
        subadmin_groups: Optional[List[str]] = None,
        quota: Optional[str] = None,
        language: Optional[str] = None,
    ) -> None:
        """
        Create a new user on the Nextcloud server.
        """
        data = {"userid": userid}
        if password is not None:
            data["password"] = password
        if display_name is not None:
            data["displayName"] = display_name
        if email is not None:
            data["email"] = email
        if groups is not None:
            for i, group in enumerate(groups):
                data[f"groups[{i}]"] = group
        if subadmin_groups is not None:
            for i, group in enumerate(subadmin_groups):
                data[f"subadmin[{i}]"] = group
        if quota is not None:
            data["quota"] = quota
        if language is not None:
            data["language"] = language

        headers = self._get_user_headers()
        await self._make_request(
            "POST", "/ocs/v2.php/cloud/users", data=data, headers=headers
        )

    async def search_users(
        self,
        search: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[str]:
        """
        Retrieves a list of users from the Nextcloud server.
        """
        params = {}
        if search is not None:
            params["search"] = search
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        headers = self._get_user_headers()
        response = await self._make_request(
            "GET", "/ocs/v2.php/cloud/users", params=params, headers=headers
        )
        # The v2 API returns JSON with users as a direct list under data.users
        data = response.json()["ocs"]["data"]
        return data.get("users", [])

    async def get_user_details(self, userid: str) -> UserDetails:
        """
        Retrieves information about a single user.
        """
        headers = self._get_user_headers()
        response = await self._make_request(
            "GET", f"/ocs/v2.php/cloud/users/{userid}", headers=headers
        )
        return UserDetails(**response.json()["ocs"]["data"])

    async def update_user_field(self, userid: str, key: str, value: str) -> None:
        """
        Edits attributes related to a user.
        """
        data = {"key": key, "value": value}
        headers = self._get_user_headers()
        await self._make_request(
            "PUT", f"/ocs/v2.php/cloud/users/{userid}", data=data, headers=headers
        )

    async def get_editable_user_fields(self) -> List[str]:
        """
        Gets the list of editable data fields for a user.
        """
        headers = self._get_user_headers()
        response = await self._make_request(
            "GET", "/ocs/v2.php/cloud/user/fields", headers=headers
        )
        # The v2 API returns data as a direct list
        data = response.json()["ocs"]["data"]
        return data if isinstance(data, list) else []

    async def disable_user(self, userid: str) -> None:
        """
        Disables a user on the Nextcloud server.
        """
        headers = self._get_user_headers()
        await self._make_request(
            "PUT", f"/ocs/v2.php/cloud/users/{userid}/disable", headers=headers
        )

    async def enable_user(self, userid: str) -> None:
        """
        Enables a user on the Nextcloud server.
        """
        headers = self._get_user_headers()
        await self._make_request(
            "PUT", f"/ocs/v2.php/cloud/users/{userid}/enable", headers=headers
        )

    async def delete_user(self, userid: str) -> None:
        """
        Deletes a user from the Nextcloud server.
        """
        headers = self._get_user_headers()
        await self._make_request(
            "DELETE", f"/ocs/v2.php/cloud/users/{userid}", headers=headers
        )

    async def get_user_groups(self, userid: str) -> List[str]:
        """
        Retrieves a list of groups the specified user is a member of.
        """
        headers = self._get_user_headers()
        response = await self._make_request(
            "GET", f"/ocs/v2.php/cloud/users/{userid}/groups", headers=headers
        )
        # The v2 API returns groups as a direct list under data.groups
        data = response.json()["ocs"]["data"]
        return data.get("groups", [])

    async def add_user_to_group(self, userid: str, groupid: str) -> None:
        """
        Adds the specified user to the specified group.
        """
        data = {"groupid": groupid}
        headers = self._get_user_headers()
        await self._make_request(
            "POST",
            f"/ocs/v2.php/cloud/users/{userid}/groups",
            data=data,
            headers=headers,
        )

    async def remove_user_from_group(self, userid: str, groupid: str) -> None:
        """
        Removes the specified user from the specified group.
        """
        data = {"groupid": groupid}
        headers = self._get_user_headers()
        await self._make_request(
            "DELETE",
            f"/ocs/v2.php/cloud/users/{userid}/groups",
            data=data,
            headers=headers,
        )

    async def promote_user_to_subadmin(self, userid: str, groupid: str) -> None:
        """
        Makes a user the subadmin of a group.
        """
        data = {"groupid": groupid}
        headers = self._get_user_headers()
        await self._make_request(
            "POST",
            f"/ocs/v2.php/cloud/users/{userid}/subadmins",
            data=data,
            headers=headers,
        )

    async def demote_user_from_subadmin(self, userid: str, groupid: str) -> None:
        """
        Removes the subadmin rights for the user specified from the group specified.
        """
        data = {"groupid": groupid}
        headers = self._get_user_headers()
        await self._make_request(
            "DELETE",
            f"/ocs/v2.php/cloud/users/{userid}/subadmins",
            data=data,
            headers=headers,
        )

    async def get_user_subadmin_groups(self, userid: str) -> List[str]:
        """
        Returns the groups in which the user is a subadmin.
        """
        headers = self._get_user_headers()
        response = await self._make_request(
            "GET", f"/ocs/v2.php/cloud/users/{userid}/subadmins", headers=headers
        )
        # The v2 API returns data as a direct list
        data = response.json()["ocs"]["data"]
        return data if isinstance(data, list) else []

    async def resend_welcome_email(self, userid: str) -> None:
        """
        Triggers the welcome email for this user again.
        """
        headers = self._get_user_headers()
        await self._make_request(
            "POST", f"/ocs/v2.php/cloud/users/{userid}/welcome", headers=headers
        )
