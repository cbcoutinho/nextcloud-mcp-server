"""Multi-user permission tests for Login Flow v2 deployment mode.

Tests verify that Nextcloud's sharing / ACL enforcement works correctly
when resources are accessed through MCP tools by different users, each
authenticated via Login Flow v2.

Ported from the removed ``tests/server/oauth/test_oauth_*_permissions.py``
tests.  The underlying assertions are deployment-mode-agnostic; only the
transport changed (OAuth MCP server -> Login Flow v2 MCP server).
"""

import json
import logging

import pytest
from mcp import ClientSession

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.login_flow]


# ---------------------------------------------------------------------------
# WebDAV / Files
# ---------------------------------------------------------------------------


class TestFilePermissions:
    """Test that MCP file tools respect Nextcloud sharing permissions."""

    async def test_file_share_read_permissions(
        self,
        alice_login_flow_mcp_client: ClientSession,
        bob_login_flow_mcp_client: ClientSession,
        diana_login_flow_mcp_client: ClientSession,
    ):
        """Alice shares a file with Bob (read-only).  Bob can read it;
        Diana (unshared) cannot."""
        file_path = "/alice_shared_file_read.txt"
        file_content = "This file is shared with Bob for reading only."

        # Alice creates the file
        result = await alice_login_flow_mcp_client.call_tool(
            "nc_webdav_write_file",
            arguments={"path": file_path, "content": file_content},
        )
        assert not result.isError, f"Alice failed to create file: {result.content}"

        share_id = None
        try:
            # Alice shares with Bob (read-only, permissions=1)
            result = await alice_login_flow_mcp_client.call_tool(
                "nc_share_create",
                arguments={
                    "path": file_path,
                    "share_with": "bob",
                    "share_type": 0,
                    "permissions": 1,
                },
            )
            assert not result.isError, f"Share creation failed: {result.content}"
            share_data = json.loads(result.content[0].text)
            share_id = share_data["id"]

            # Bob reads the file
            result = await bob_login_flow_mcp_client.call_tool(
                "nc_webdav_read_file", arguments={"path": file_path}
            )
            assert not result.isError, (
                f"Bob could not read shared file: {result.content}"
            )
            response_data = json.loads(result.content[0].text)
            assert file_content in response_data["content"]

            # Diana cannot read the file
            result = await diana_login_flow_mcp_client.call_tool(
                "nc_webdav_read_file", arguments={"path": file_path}
            )
            assert result.isError, "Diana should not be able to read unshared file"

        finally:
            if share_id:
                await alice_login_flow_mcp_client.call_tool(
                    "nc_share_delete", arguments={"share_id": share_id}
                )
            await alice_login_flow_mcp_client.call_tool(
                "nc_webdav_delete_resource", arguments={"path": file_path}
            )

    async def test_file_share_write_permissions(
        self,
        alice_login_flow_mcp_client: ClientSession,
        charlie_login_flow_mcp_client: ClientSession,
        bob_login_flow_mcp_client: ClientSession,
    ):
        """Alice shares a file with Charlie (edit) and Bob (read-only).
        Charlie can overwrite; Bob cannot."""
        file_path = "/alice_shared_file_write.txt"
        file_content = "This file is shared with Charlie for editing."

        result = await alice_login_flow_mcp_client.call_tool(
            "nc_webdav_write_file",
            arguments={"path": file_path, "content": file_content},
        )
        assert not result.isError

        charlie_share_id = None
        bob_share_id = None
        try:
            # Share with Charlie (read+write, permissions=3)
            result = await alice_login_flow_mcp_client.call_tool(
                "nc_share_create",
                arguments={
                    "path": file_path,
                    "share_with": "charlie",
                    "share_type": 0,
                    "permissions": 3,
                },
            )
            assert not result.isError
            charlie_share_id = json.loads(result.content[0].text)["id"]

            # Share with Bob (read-only, permissions=1)
            result = await alice_login_flow_mcp_client.call_tool(
                "nc_share_create",
                arguments={
                    "path": file_path,
                    "share_with": "bob",
                    "share_type": 0,
                    "permissions": 1,
                },
            )
            assert not result.isError
            bob_share_id = json.loads(result.content[0].text)["id"]

            # Charlie can write
            result = await charlie_login_flow_mcp_client.call_tool(
                "nc_webdav_write_file",
                arguments={
                    "path": file_path,
                    "content": f"{file_content}\nCharlie added this line.",
                },
            )
            assert not result.isError, (
                f"Charlie should be able to write: {result.content}"
            )

            # Bob cannot write
            result = await bob_login_flow_mcp_client.call_tool(
                "nc_webdav_write_file",
                arguments={
                    "path": file_path,
                    "content": "Bob tries to overwrite this.",
                },
            )
            assert result.isError, "Bob should be denied write access (read-only)"

        finally:
            for sid in (charlie_share_id, bob_share_id):
                if sid:
                    await alice_login_flow_mcp_client.call_tool(
                        "nc_share_delete", arguments={"share_id": sid}
                    )
            await alice_login_flow_mcp_client.call_tool(
                "nc_webdav_delete_resource", arguments={"path": file_path}
            )

    async def test_folder_share_permissions(
        self,
        alice_login_flow_mcp_client: ClientSession,
        bob_login_flow_mcp_client: ClientSession,
    ):
        """Alice shares a folder with Bob; Bob can list and read its contents."""
        folder_path = "/alice_shared_folder"
        file_in_folder = f"{folder_path}/document.txt"
        file_content = "Document in Alice's shared folder"

        result = await alice_login_flow_mcp_client.call_tool(
            "nc_webdav_create_directory", arguments={"path": folder_path}
        )
        assert not result.isError

        result = await alice_login_flow_mcp_client.call_tool(
            "nc_webdav_write_file",
            arguments={"path": file_in_folder, "content": file_content},
        )
        assert not result.isError

        share_id = None
        try:
            result = await alice_login_flow_mcp_client.call_tool(
                "nc_share_create",
                arguments={
                    "path": folder_path,
                    "share_with": "bob",
                    "share_type": 0,
                    "permissions": 1,
                },
            )
            assert not result.isError
            share_id = json.loads(result.content[0].text)["id"]

            # Bob lists the shared folder
            result = await bob_login_flow_mcp_client.call_tool(
                "nc_webdav_list_directory", arguments={"path": folder_path}
            )
            assert not result.isError, f"Bob should see shared folder: {result.content}"
            response_data = json.loads(result.content[0].text)
            file_names = [f["name"] for f in response_data.get("files", [])]
            assert "document.txt" in file_names

            # Bob reads the file
            result = await bob_login_flow_mcp_client.call_tool(
                "nc_webdav_read_file", arguments={"path": file_in_folder}
            )
            assert not result.isError
            assert file_content in json.loads(result.content[0].text)["content"]

        finally:
            if share_id:
                await alice_login_flow_mcp_client.call_tool(
                    "nc_share_delete", arguments={"share_id": share_id}
                )
            await alice_login_flow_mcp_client.call_tool(
                "nc_webdav_delete_resource", arguments={"path": folder_path}
            )

    async def test_user_isolation_files(
        self,
        alice_login_flow_mcp_client: ClientSession,
        bob_login_flow_mcp_client: ClientSession,
    ):
        """Users can only see their own files when nothing is shared."""
        alice_file = "/alice_private_file.txt"
        bob_file = "/bob_private_file.txt"

        # Each user creates their own file
        result = await alice_login_flow_mcp_client.call_tool(
            "nc_webdav_write_file",
            arguments={"path": alice_file, "content": "Alice's private file"},
        )
        assert not result.isError

        result = await bob_login_flow_mcp_client.call_tool(
            "nc_webdav_write_file",
            arguments={"path": bob_file, "content": "Bob's private file"},
        )
        assert not result.isError

        try:
            # Bob lists root — should NOT see Alice's file
            result = await bob_login_flow_mcp_client.call_tool(
                "nc_webdav_list_directory", arguments={"path": "/"}
            )
            assert not result.isError
            bob_visible = [
                f["name"] for f in json.loads(result.content[0].text).get("files", [])
            ]
            assert "alice_private_file.txt" not in bob_visible, (
                "Bob should not see Alice's private file"
            )

            # Alice lists root — should NOT see Bob's file
            result = await alice_login_flow_mcp_client.call_tool(
                "nc_webdav_list_directory", arguments={"path": "/"}
            )
            assert not result.isError
            alice_visible = [
                f["name"] for f in json.loads(result.content[0].text).get("files", [])
            ]
            assert "bob_private_file.txt" not in alice_visible, (
                "Alice should not see Bob's private file"
            )

        finally:
            await alice_login_flow_mcp_client.call_tool(
                "nc_webdav_delete_resource", arguments={"path": alice_file}
            )
            await bob_login_flow_mcp_client.call_tool(
                "nc_webdav_delete_resource", arguments={"path": bob_file}
            )


# ---------------------------------------------------------------------------
# Deck
# ---------------------------------------------------------------------------


class TestDeckPermissions:
    """Test that MCP Deck tools respect board ACL permissions."""

    async def _add_board_acl(
        self, nc_client, board_id: int, user: str, permission_type: int = 0
    ) -> int:
        """Add ACL entry.  permission_type: 0=view, 1=edit, 2=manage."""
        acl = await nc_client.deck.add_acl_rule(
            board_id=board_id,
            type=0,
            participant=user,
            permission_edit=permission_type >= 1,
            permission_share=permission_type >= 2,
            permission_manage=permission_type >= 2,
        )
        return acl.id

    async def test_deck_board_view_permissions(
        self,
        nc_client,
        alice_login_flow_mcp_client: ClientSession,
        bob_login_flow_mcp_client: ClientSession,
        diana_login_flow_mcp_client: ClientSession,
    ):
        """Admin creates a board, adds Bob (view). Bob can see it; Diana cannot."""
        board = await nc_client.deck.create_board("Shared Board - View Test", "FF0000")
        board_id = board.id
        bob_acl_id = None

        try:
            bob_acl_id = await self._add_board_acl(nc_client, board_id, "bob", 0)

            # Bob can see the board
            result = await bob_login_flow_mcp_client.call_tool(
                "deck_get_boards", arguments={}
            )
            assert not result.isError
            board_ids = [
                b["id"] for b in json.loads(result.content[0].text).get("boards", [])
            ]
            assert board_id in board_ids, "Bob should see shared board"

            # Diana cannot see the board
            result = await diana_login_flow_mcp_client.call_tool(
                "deck_get_boards", arguments={}
            )
            assert not result.isError
            board_ids = [
                b["id"] for b in json.loads(result.content[0].text).get("boards", [])
            ]
            assert board_id not in board_ids, "Diana should not see board without ACL"

        finally:
            if bob_acl_id:
                await nc_client.deck.delete_acl_rule(board_id, bob_acl_id)
            await nc_client.deck.delete_board(board_id)

    async def test_deck_board_edit_permissions(
        self,
        nc_client,
        charlie_login_flow_mcp_client: ClientSession,
        bob_login_flow_mcp_client: ClientSession,
    ):
        """Charlie (edit) can create cards; Bob (view-only) cannot."""
        board = await nc_client.deck.create_board("Shared Board - Edit Test", "00FF00")
        board_id = board.id
        stack = await nc_client.deck.create_stack(board_id, "Test Stack", 1)
        stack_id = stack.id
        charlie_acl_id = None
        bob_acl_id = None

        try:
            charlie_acl_id = await self._add_board_acl(
                nc_client, board_id, "charlie", 1
            )
            bob_acl_id = await self._add_board_acl(nc_client, board_id, "bob", 0)

            # Charlie creates a card
            result = await charlie_login_flow_mcp_client.call_tool(
                "deck_create_card",
                arguments={
                    "board_id": board_id,
                    "stack_id": stack_id,
                    "title": "Charlie's Card",
                    "description": "Created by Charlie with edit permission",
                },
            )
            assert not result.isError, f"Charlie should create cards: {result.content}"
            card_id = json.loads(result.content[0].text).get("id")
            if card_id:
                await nc_client.deck.delete_card(board_id, stack_id, card_id)

            # Bob cannot create a card
            result = await bob_login_flow_mcp_client.call_tool(
                "deck_create_card",
                arguments={
                    "board_id": board_id,
                    "stack_id": stack_id,
                    "title": "Bob's Card",
                    "description": "Bob trying to create a card",
                },
            )
            assert result.isError, "Bob should be denied card creation (view-only)"

        finally:
            for acl_id in (charlie_acl_id, bob_acl_id):
                if acl_id:
                    await nc_client.deck.delete_acl_rule(board_id, acl_id)
            await nc_client.deck.delete_board(board_id)

    async def test_deck_user_isolation(
        self,
        nc_client,
        alice_login_flow_mcp_client: ClientSession,
        bob_login_flow_mcp_client: ClientSession,
    ):
        """Users can only see their own boards when nothing is shared."""
        alice_board = await nc_client.deck.create_board(
            "Alice's Private Board", "FF00FF"
        )
        bob_board = await nc_client.deck.create_board("Bob's Private Board", "00FFFF")

        try:
            # Alice should NOT see Bob's board
            result = await alice_login_flow_mcp_client.call_tool(
                "deck_get_boards", arguments={}
            )
            assert not result.isError
            board_ids = [
                b["id"] for b in json.loads(result.content[0].text).get("boards", [])
            ]
            assert bob_board.id not in board_ids, (
                "Alice should not see Bob's private board"
            )

            # Bob should NOT see Alice's board
            result = await bob_login_flow_mcp_client.call_tool(
                "deck_get_boards", arguments={}
            )
            assert not result.isError
            board_ids = [
                b["id"] for b in json.loads(result.content[0].text).get("boards", [])
            ]
            assert alice_board.id not in board_ids, (
                "Bob should not see Alice's private board"
            )

        finally:
            await nc_client.deck.delete_board(alice_board.id)
            await nc_client.deck.delete_board(bob_board.id)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


class TestNotesPermissions:
    """Test that MCP Notes tools respect user isolation.

    Nextcloud Notes are inherently single-user (no sharing API). These tests
    verify that notes created by one user are invisible to others.
    """

    async def test_user_isolation_notes(
        self,
        alice_login_flow_mcp_client: ClientSession,
        bob_login_flow_mcp_client: ClientSession,
    ):
        """Notes created by Alice are invisible to Bob and vice versa."""
        # Alice creates a note
        result = await alice_login_flow_mcp_client.call_tool(
            "nc_notes_create_note",
            arguments={
                "title": "Alice's Private Note",
                "content": "This is Alice's private content.",
                "category": "PermTest",
            },
        )
        assert not result.isError
        alice_note_id = json.loads(result.content[0].text)["id"]

        # Bob creates a note
        result = await bob_login_flow_mcp_client.call_tool(
            "nc_notes_create_note",
            arguments={
                "title": "Bob's Private Note",
                "content": "This is Bob's private content.",
                "category": "PermTest",
            },
        )
        assert not result.isError
        bob_note_id = json.loads(result.content[0].text)["id"]

        try:
            # Alice searches — should NOT see Bob's note
            result = await alice_login_flow_mcp_client.call_tool(
                "nc_notes_search_notes", arguments={"query": "PermTest"}
            )
            assert not result.isError
            alice_visible_ids = [
                n["id"] for n in json.loads(result.content[0].text).get("results", [])
            ]
            assert bob_note_id not in alice_visible_ids, (
                "Alice should not see Bob's private note"
            )

            # Bob searches — should NOT see Alice's note
            result = await bob_login_flow_mcp_client.call_tool(
                "nc_notes_search_notes", arguments={"query": "PermTest"}
            )
            assert not result.isError
            bob_visible_ids = [
                n["id"] for n in json.loads(result.content[0].text).get("results", [])
            ]
            assert alice_note_id not in bob_visible_ids, (
                "Bob should not see Alice's private note"
            )

        finally:
            await alice_login_flow_mcp_client.call_tool(
                "nc_notes_delete_note", arguments={"note_id": alice_note_id}
            )
            await bob_login_flow_mcp_client.call_tool(
                "nc_notes_delete_note", arguments={"note_id": bob_note_id}
            )


# ---------------------------------------------------------------------------
# Smoke: all multi-user clients initialised
# ---------------------------------------------------------------------------


class TestMultiUserSmoke:
    """Quick check that all multi-user MCP clients are functional."""

    async def test_all_clients_can_list_tools(
        self,
        alice_login_flow_mcp_client: ClientSession,
        bob_login_flow_mcp_client: ClientSession,
        charlie_login_flow_mcp_client: ClientSession,
        diana_login_flow_mcp_client: ClientSession,
    ):
        for name, client in [
            ("alice", alice_login_flow_mcp_client),
            ("bob", bob_login_flow_mcp_client),
            ("charlie", charlie_login_flow_mcp_client),
            ("diana", diana_login_flow_mcp_client),
        ]:
            tools = await client.list_tools()
            assert len(tools.tools) > 0, f"{name} MCP client has no tools"
            logger.info(f"{name} MCP client working ({len(tools.tools)} tools)")
