from typing import Any, Dict, List, Optional

from nextcloud_mcp_server.client.base import BaseNextcloudClient
from nextcloud_mcp_server.models.deck import (
    DeckACL,
    DeckAttachment,
    DeckBoard,
    DeckCard,
    DeckComment,
    DeckConfig,
    DeckLabel,
    DeckSession,
    DeckStack,
)


class DeckClient(BaseNextcloudClient):
    """Client for Nextcloud Deck app operations."""

    def _get_deck_headers(
        self, additional_headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """Get standard headers required for Deck API calls."""
        headers = {"OCS-APIRequest": "true", "Content-Type": "application/json"}
        if additional_headers:
            headers.update(additional_headers)
        return headers

    # Boards
    async def get_boards(
        self, details: bool = False, if_modified_since: Optional[str] = None
    ) -> List[DeckBoard]:
        additional_headers = {}
        if if_modified_since:
            additional_headers["If-Modified-Since"] = if_modified_since
        headers = self._get_deck_headers(additional_headers)
        params = {"details": "true"} if details else {}
        response = await self._make_request(
            "GET", "/apps/deck/api/v1.0/boards", headers=headers, params=params
        )
        return [DeckBoard(**board) for board in response.json()]

    async def create_board(self, title: str, color: str) -> DeckBoard:
        json_data = {"title": title, "color": color}
        headers = self._get_deck_headers()
        response = await self._make_request(
            "POST", "/apps/deck/api/v1.0/boards", json=json_data, headers=headers
        )
        return DeckBoard(**response.json())

    async def get_board(self, board_id: int) -> DeckBoard:
        headers = self._get_deck_headers()
        response = await self._make_request(
            "GET", f"/apps/deck/api/v1.0/boards/{board_id}", headers=headers
        )
        return DeckBoard(**response.json())

    async def update_board(
        self,
        board_id: int,
        title: Optional[str] = None,
        color: Optional[str] = None,
        archived: Optional[bool] = None,
    ) -> None:
        json_data = {}
        if title is not None:
            json_data["title"] = title
        if color is not None:
            json_data["color"] = color
        if archived is not None:
            json_data["archived"] = archived
        headers = self._get_deck_headers()
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}",
            json=json_data,
            headers=headers,
        )

    async def delete_board(self, board_id: int) -> None:
        headers = self._get_deck_headers()
        await self._make_request(
            "DELETE", f"/apps/deck/api/v1.0/boards/{board_id}", headers=headers
        )

    async def undo_delete_board(self, board_id: int) -> None:
        headers = self._get_deck_headers()
        await self._make_request(
            "POST",
            f"/apps/deck/api/v1.0/boards/{board_id}/undo_delete",
            headers=headers,
        )

    async def add_acl_rule(
        self,
        board_id: int,
        type: int,
        participant: str,
        permission_edit: bool,
        permission_share: bool,
        permission_manage: bool,
    ) -> DeckACL:
        json_data = {
            "type": type,
            "participant": participant,
            "permissionEdit": permission_edit,
            "permissionShare": permission_share,
            "permissionManage": permission_manage,
        }
        headers = self._get_deck_headers()
        response = await self._make_request(
            "POST",
            f"/apps/deck/api/v1.0/boards/{board_id}/acl",
            json=json_data,
            headers=headers,
        )
        return DeckACL(**response.json())

    async def update_acl_rule(
        self,
        board_id: int,
        acl_id: int,
        permission_edit: Optional[bool] = None,
        permission_share: Optional[bool] = None,
        permission_manage: Optional[bool] = None,
    ) -> None:
        json_data = {}
        if permission_edit is not None:
            json_data["permissionEdit"] = permission_edit
        if permission_share is not None:
            json_data["permissionShare"] = permission_share
        if permission_manage is not None:
            json_data["permissionManage"] = permission_manage
        headers = self._get_deck_headers()
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/acl/{acl_id}",
            json=json_data,
            headers=headers,
        )

    async def delete_acl_rule(self, board_id: int, acl_id: int) -> None:
        headers = self._get_deck_headers()
        await self._make_request(
            "DELETE",
            f"/apps/deck/api/v1.0/boards/{board_id}/acl/{acl_id}",
            headers=headers,
        )

    async def clone_board(
        self,
        board_id: int,
        with_cards: bool = False,
        with_assignments: bool = False,
        with_labels: bool = False,
        with_due_date: bool = False,
        move_cards_to_left_stack: bool = False,
        restore_archived_cards: bool = False,
    ) -> DeckBoard:
        json_data = {
            "withCards": with_cards,
            "withAssignments": with_assignments,
            "withLabels": with_labels,
            "withDueDate": with_due_date,
            "moveCardsToLeftStack": move_cards_to_left_stack,
            "restoreArchivedCards": restore_archived_cards,
        }
        response = await self._make_request(
            "POST", f"/apps/deck/api/v1.0/boards/{board_id}/clone", json=json_data
        )
        return DeckBoard(**response.json())

    # Stacks
    async def get_stacks(
        self, board_id: int, if_modified_since: Optional[str] = None
    ) -> List[DeckStack]:
        additional_headers = {}
        if if_modified_since:
            additional_headers["If-Modified-Since"] = if_modified_since
        headers = self._get_deck_headers(additional_headers)
        response = await self._make_request(
            "GET", f"/apps/deck/api/v1.0/boards/{board_id}/stacks", headers=headers
        )
        return [DeckStack(**stack) for stack in response.json()]

    async def get_archived_stacks(self, board_id: int) -> List[DeckStack]:
        response = await self._make_request(
            "GET", f"/apps/deck/api/v1.0/boards/{board_id}/stacks/archived"
        )
        return [DeckStack(**stack) for stack in response.json()]

    async def get_stack(self, board_id: int, stack_id: int) -> DeckStack:
        response = await self._make_request(
            "GET", f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}"
        )
        return DeckStack(**response.json())

    async def create_stack(self, board_id: int, title: str, order: int) -> DeckStack:
        json_data = {"title": title, "order": order}
        headers = self._get_deck_headers()
        response = await self._make_request(
            "POST",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks",
            json=json_data,
            headers=headers,
        )
        return DeckStack(**response.json())

    async def update_stack(
        self,
        board_id: int,
        stack_id: int,
        title: Optional[str] = None,
        order: Optional[int] = None,
    ) -> None:
        json_data = {}
        if title is not None:
            json_data["title"] = title
        if order is not None:
            json_data["order"] = order
        headers = self._get_deck_headers()
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}",
            json=json_data,
            headers=headers,
        )

    async def delete_stack(self, board_id: int, stack_id: int) -> None:
        await self._make_request(
            "DELETE", f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}"
        )

    # Cards
    async def get_card(self, board_id: int, stack_id: int, card_id: int) -> DeckCard:
        headers = self._get_deck_headers()
        response = await self._make_request(
            "GET",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}",
            headers=headers,
        )
        return DeckCard(**response.json())

    async def create_card(
        self,
        board_id: int,
        stack_id: int,
        title: str,
        type: str = "plain",
        order: int = 999,
        description: Optional[str] = None,
        duedate: Optional[str] = None,
    ) -> DeckCard:
        json_data = {
            "title": title,
            "type": type,
            "order": order,
        }
        if description is not None:
            json_data["description"] = description
        if duedate is not None:
            json_data["duedate"] = duedate
        headers = self._get_deck_headers()
        response = await self._make_request(
            "POST",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards",
            json=json_data,
            headers=headers,
        )
        return DeckCard(**response.json())

    async def update_card(
        self,
        board_id: int,
        stack_id: int,
        card_id: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        type: Optional[str] = None,
        owner: Optional[str] = None,
        order: Optional[int] = None,
        duedate: Optional[str] = None,
        archived: Optional[bool] = None,
        done: Optional[str] = None,
    ) -> None:
        # First, get the current card to use existing values for required fields
        current_card = await self.get_card(board_id, stack_id, card_id)

        json_data = {}
        if title is not None:
            json_data["title"] = title
        if description is not None:
            json_data["description"] = description
        # Type is required by the API, use provided or keep current
        json_data["type"] = type if type is not None else current_card.type
        # Owner is required by the API, use provided or keep current
        json_data["owner"] = (
            owner
            if owner is not None
            else (
                current_card.owner
                if isinstance(current_card.owner, str)
                else current_card.owner.uid
                if hasattr(current_card.owner, "uid")
                else current_card.owner.primaryKey
            )
        )
        if order is not None:
            json_data["order"] = order
        if duedate is not None:
            json_data["duedate"] = duedate
        if archived is not None:
            json_data["archived"] = archived
        if done is not None:
            json_data["done"] = done
        headers = self._get_deck_headers()
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}",
            json=json_data,
            headers=headers,
        )

    async def delete_card(self, board_id: int, stack_id: int, card_id: int) -> None:
        headers = self._get_deck_headers()
        await self._make_request(
            "DELETE",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}",
            headers=headers,
        )

    async def archive_card(self, board_id: int, stack_id: int, card_id: int) -> None:
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/archive",
        )

    async def unarchive_card(self, board_id: int, stack_id: int, card_id: int) -> None:
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/unarchive",
        )

    async def assign_label_to_card(
        self, board_id: int, stack_id: int, card_id: int, label_id: int
    ) -> None:
        json_data = {"labelId": label_id}
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/assignLabel",
            json=json_data,
        )

    async def remove_label_from_card(
        self, board_id: int, stack_id: int, card_id: int, label_id: int
    ) -> None:
        json_data = {"labelId": label_id}
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/removeLabel",
            json=json_data,
        )

    async def assign_user_to_card(
        self, board_id: int, stack_id: int, card_id: int, user_id: str
    ) -> None:
        json_data = {"userId": user_id}
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/assignUser",
            json=json_data,
        )

    async def unassign_user_from_card(
        self, board_id: int, stack_id: int, card_id: int, user_id: str
    ) -> None:
        json_data = {"userId": user_id}
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/unassignUser",
            json=json_data,
        )

    async def reorder_card(
        self,
        board_id: int,
        stack_id: int,
        card_id: int,
        order: int,
        target_stack_id: int,
    ) -> None:
        json_data = {"order": order, "stackId": target_stack_id}
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/reorder",
            json=json_data,
        )

    # Labels
    async def get_label(self, board_id: int, label_id: int) -> DeckLabel:
        headers = self._get_deck_headers()
        response = await self._make_request(
            "GET",
            f"/apps/deck/api/v1.0/boards/{board_id}/labels/{label_id}",
            headers=headers,
        )
        return DeckLabel(**response.json())

    async def create_label(self, board_id: int, title: str, color: str) -> DeckLabel:
        json_data = {"title": title, "color": color}
        headers = self._get_deck_headers()
        response = await self._make_request(
            "POST",
            f"/apps/deck/api/v1.0/boards/{board_id}/labels",
            json=json_data,
            headers=headers,
        )
        return DeckLabel(**response.json())

    async def update_label(
        self,
        board_id: int,
        label_id: int,
        title: Optional[str] = None,
        color: Optional[str] = None,
    ) -> None:
        json_data = {}
        if title is not None:
            json_data["title"] = title
        if color is not None:
            json_data["color"] = color
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/labels/{label_id}",
            json=json_data,
        )

    async def delete_label(self, board_id: int, label_id: int) -> None:
        await self._make_request(
            "DELETE", f"/apps/deck/api/v1.0/boards/{board_id}/labels/{label_id}"
        )

    # Attachments
    async def get_attachments(
        self, board_id: int, stack_id: int, card_id: int
    ) -> List[DeckAttachment]:
        response = await self._make_request(
            "GET",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/attachments",
        )
        return [DeckAttachment(**attachment) for attachment in response.json()]

    async def get_attachment_file(
        self, board_id: int, stack_id: int, card_id: int, attachment_id: int
    ) -> Any:
        # This endpoint returns the raw file, so we return the raw response content
        response = await self._make_request(
            "GET",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/attachments/{attachment_id}",
        )
        return response.content

    async def upload_attachment(
        self,
        board_id: int,
        stack_id: int,
        card_id: int,
        file_data: bytes,
        file_type: str = "file",
    ) -> DeckAttachment:
        # The API expects binary data directly, not JSON
        headers = {"Content-Type": "application/octet-stream"}
        params = {"type": file_type}
        response = await self._make_request(
            "POST",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/attachments",
            headers=headers,
            params=params,
            data=file_data,
        )
        return DeckAttachment(**response.json())

    async def update_attachment(
        self,
        board_id: int,
        stack_id: int,
        card_id: int,
        attachment_id: int,
        file_data: bytes,
        file_type: str = "deck_file",
    ) -> DeckAttachment:
        headers = {"Content-Type": "application/octet-stream"}
        params = {"type": file_type}
        response = await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/attachments/{attachment_id}",
            headers=headers,
            params=params,
            data=file_data,
        )
        return DeckAttachment(**response.json())

    async def delete_attachment(
        self, board_id: int, stack_id: int, card_id: int, attachment_id: int
    ) -> None:
        await self._make_request(
            "DELETE",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/attachments/{attachment_id}",
        )

    async def restore_attachment(
        self, board_id: int, stack_id: int, card_id: int, attachment_id: int
    ) -> None:
        await self._make_request(
            "PUT",
            f"/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/attachments/{attachment_id}/restore",
        )

    # OCS API Endpoints (Config, Comments, Sessions)
    async def get_config(self) -> DeckConfig:
        headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
        response = await self._make_request(
            "GET", "/ocs/v2.php/apps/deck/api/v1.0/config", headers=headers
        )
        return DeckConfig(**response.json()["ocs"]["data"])

    async def set_config_value(
        self, key: str, value: Any, board_id: Optional[int] = None
    ) -> Any:
        path = f"/ocs/v2.php/apps/deck/api/v1.0/config/{key}"
        if board_id:
            path = f"/ocs/v2.php/apps/deck/api/v1.0/config/board:{board_id}:{key}"
        json_data = {"value": value}
        response = await self._make_request(
            "POST",
            path,
            json=json_data,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        return response.json()["ocs"]["data"]

    async def get_comments(
        self, card_id: int, limit: int = 20, offset: int = 0
    ) -> List[DeckComment]:
        params = {"limit": limit, "offset": offset}
        response = await self._make_request(
            "GET",
            f"/ocs/v2.php/apps/deck/api/v1.0/cards/{card_id}/comments",
            params=params,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        return [DeckComment(**comment) for comment in response.json()["ocs"]["data"]]

    async def create_comment(
        self, card_id: int, message: str, parent_id: Optional[int] = None
    ) -> DeckComment:
        json_data = {"message": message}
        if parent_id is not None:
            json_data["parentId"] = parent_id
        response = await self._make_request(
            "POST",
            f"/ocs/v2.php/apps/deck/api/v1.0/cards/{card_id}/comments",
            json=json_data,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        return DeckComment(**response.json()["ocs"]["data"])

    async def update_comment(
        self, card_id: int, comment_id: int, message: str
    ) -> DeckComment:
        json_data = {"message": message}
        response = await self._make_request(
            "PUT",
            f"/ocs/v2.php/apps/deck/api/v1.0/cards/{card_id}/comments/{comment_id}",
            json=json_data,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        return DeckComment(**response.json()["ocs"]["data"])

    async def delete_comment(self, card_id: int, comment_id: int) -> None:
        await self._make_request(
            "DELETE",
            f"/ocs/v2.php/apps/deck/api/v1.0/cards/{card_id}/comments/{comment_id}",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )

    async def create_session(self, board_id: int) -> DeckSession:
        json_data = {"boardId": board_id}
        response = await self._make_request(
            "PUT",
            "/ocs/v2.php/apps/deck/api/v1.0/session/create",
            json=json_data,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        return DeckSession(**response.json()["ocs"]["data"])

    async def sync_session(self, board_id: int, token: str) -> None:
        json_data = {"boardId": board_id, "token": token}
        await self._make_request(
            "POST",
            "/ocs/v2.php/apps/deck/api/v1.0/session/sync",
            json=json_data,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )

    async def close_session(self, board_id: int, token: str) -> None:
        json_data = {"boardId": board_id, "token": token}
        await self._make_request(
            "POST",
            "/ocs/v2.php/apps/deck/api/v1.0/session/close",
            json=json_data,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
