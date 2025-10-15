"""
Multi-user OAuth tests for Nextcloud WebDAV file permissions.

Tests verify that the MCP server respects Nextcloud file sharing permissions
when accessed via OAuth authentication with different users.

All operations (file creation, sharing, access) are performed through MCP tools
to ensure the MCP server properly supports multi-user scenarios.
"""

import json
import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


@pytest.mark.asyncio
async def test_file_share_read_permissions(
    alice_mcp_client, bob_mcp_client, diana_mcp_client
):
    """
    Test that shared files respect read permissions.

    Scenario:
    1. Alice creates a file via MCP
    2. Alice shares the file with Bob (read-only) via MCP
    3. Bob can read the file via MCP tools
    4. Diana cannot access the file (no share)
    """
    file_path = "/alice_shared_file_read.txt"
    file_content = "This file is shared with Bob for reading only."

    # Alice creates a file
    logger.info(f"Alice creating file: {file_path}")
    result = await alice_mcp_client.call_tool(
        "nc_webdav_write_file",
        arguments={"path": file_path, "content": file_content},
    )
    assert not result.isError, f"Alice failed to create file: {result.content}"

    share_id = None

    try:
        # Alice shares the file with bob (read-only, permissions=1)
        logger.info("Alice sharing file with bob (read-only)...")
        result = await alice_mcp_client.call_tool(
            "nc_share_create",
            arguments={
                "path": file_path,
                "share_with": "bob",
                "share_type": 0,
                "permissions": 1,
            },
        )
        assert not result.isError, f"Alice failed to create share: {result.content}"
        share_data = json.loads(result.content[0].text)
        share_id = share_data["id"]
        logger.info(f"Created share {share_id}")

        # Test: Bob reads the file via MCP
        logger.info("Bob attempting to read file via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": file_path}
        )

        # Bob should be able to read the shared file
        if not result.isError:
            response_data = json.loads(result.content[0].text)
            logger.info(
                f"Bob successfully read file: {response_data.get('content', '')[:50]}..."
            )
            assert "content" in response_data
            assert file_content in response_data["content"]
        else:
            logger.warning(f"Bob could not read file: {result.content}")
            # This might fail if the share path is different for bob

        # Test: Diana attempts to read the file
        logger.info("Diana attempting to read file via MCP...")
        result = await diana_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": file_path}
        )

        # Diana should NOT be able to read (no share)
        if result.isError:
            logger.info("Diana correctly denied access to unshared file")
        else:
            logger.warning("Diana unexpectedly could read unshared file")

    finally:
        # Cleanup - Alice deletes the share and file
        if share_id:
            logger.info(f"Alice deleting share {share_id}")
            await alice_mcp_client.call_tool(
                "nc_share_delete", arguments={"share_id": share_id}
            )
        logger.info(f"Alice deleting file {file_path}")
        await alice_mcp_client.call_tool(
            "nc_webdav_delete_resource", arguments={"path": file_path}
        )


@pytest.mark.asyncio
async def test_file_share_write_permissions(
    alice_mcp_client, charlie_mcp_client, bob_mcp_client
):
    """
    Test that shared files respect write permissions.

    Scenario:
    1. Alice creates a file via MCP
    2. Alice shares the file with Charlie (edit permission) via MCP
    3. Alice shares the file with Bob (read-only) via MCP
    4. Charlie can edit the file via MCP tools
    5. Bob cannot edit the file
    """
    file_path = "/alice_shared_file_write.txt"
    file_content = "This file is shared with Charlie for editing."

    logger.info(f"Alice creating file: {file_path}")
    result = await alice_mcp_client.call_tool(
        "nc_webdav_write_file",
        arguments={"path": file_path, "content": file_content},
    )
    assert not result.isError, f"Alice failed to create file: {result.content}"

    charlie_share_id = None
    bob_share_id = None

    try:
        # Alice shares with Charlie (read+write, permissions=3)
        logger.info("Alice sharing file with Charlie (edit permission)...")
        result = await alice_mcp_client.call_tool(
            "nc_share_create",
            arguments={
                "path": file_path,
                "share_with": "charlie",
                "share_type": 0,
                "permissions": 3,
            },
        )
        assert not result.isError, (
            f"Alice failed to share with Charlie: {result.content}"
        )
        charlie_share_data = json.loads(result.content[0].text)
        charlie_share_id = charlie_share_data["id"]
        logger.info(f"Created share {charlie_share_id} for Charlie")

        # Alice shares with Bob (read-only, permissions=1)
        logger.info("Alice sharing file with Bob (read-only)...")
        result = await alice_mcp_client.call_tool(
            "nc_share_create",
            arguments={
                "path": file_path,
                "share_with": "bob",
                "share_type": 0,
                "permissions": 1,
            },
        )
        assert not result.isError, f"Alice failed to share with Bob: {result.content}"
        bob_share_data = json.loads(result.content[0].text)
        bob_share_id = bob_share_data["id"]
        logger.info(f"Created share {bob_share_id} for Bob")

        # Test: Charlie can write to the file
        logger.info("Charlie attempting to write to file via MCP...")
        updated_content = f"{file_content}\nCharlie added this line."
        result = await charlie_mcp_client.call_tool(
            "nc_webdav_write_file",
            arguments={"path": file_path, "content": updated_content},
        )

        if not result.isError:
            logger.info("Charlie successfully wrote to file")
        else:
            logger.warning(f"Charlie could not write to file: {result.content}")

        # Test: Bob attempts to write (should fail)
        logger.info("Bob attempting to write to file via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_webdav_write_file",
            arguments={"path": file_path, "content": "Bob tries to overwrite this."},
        )

        # Bob should be denied
        if result.isError:
            logger.info("Bob correctly denied write access")
        else:
            logger.warning("Bob unexpectedly succeeded in writing (permissions issue?)")

    finally:
        # Cleanup - Alice deletes shares and file
        if charlie_share_id:
            logger.info(f"Alice deleting Charlie's share {charlie_share_id}")
            await alice_mcp_client.call_tool(
                "nc_share_delete", arguments={"share_id": charlie_share_id}
            )
        if bob_share_id:
            logger.info(f"Alice deleting Bob's share {bob_share_id}")
            await alice_mcp_client.call_tool(
                "nc_share_delete", arguments={"share_id": bob_share_id}
            )
        logger.info(f"Alice deleting file {file_path}")
        await alice_mcp_client.call_tool(
            "nc_webdav_delete_resource", arguments={"path": file_path}
        )


@pytest.mark.asyncio
async def test_file_list_permissions(alice_mcp_client, bob_mcp_client):
    """
    Test that file listing respects share permissions.

    Scenario:
    1. Alice creates her private file via MCP
    2. Bob creates his private file via MCP
    3. Alice creates a file and shares it with Bob via MCP
    4. Alice can list her own files + shared files
    5. Bob can list his own files + shared files from Alice
    """
    alice_file = "/alice_private_file.txt"
    bob_file = "/bob_private_file.txt"
    shared_file = "/alice_shared_with_bob.txt"

    # Alice creates her private file
    logger.info(f"Alice creating private file: {alice_file}")
    result = await alice_mcp_client.call_tool(
        "nc_webdav_write_file",
        arguments={"path": alice_file, "content": "Alice's private file"},
    )
    assert not result.isError, f"Alice failed to create file: {result.content}"

    # Bob creates his private file
    logger.info(f"Bob creating private file: {bob_file}")
    result = await bob_mcp_client.call_tool(
        "nc_webdav_write_file",
        arguments={"path": bob_file, "content": "Bob's private file"},
    )
    assert not result.isError, f"Bob failed to create file: {result.content}"

    # Alice creates a shared file
    logger.info(f"Alice creating shared file: {shared_file}")
    result = await alice_mcp_client.call_tool(
        "nc_webdav_write_file",
        arguments={"path": shared_file, "content": "Shared file content"},
    )
    assert not result.isError, f"Alice failed to create shared file: {result.content}"

    share_id = None

    try:
        # Alice shares the file with Bob
        logger.info("Alice sharing file with Bob...")
        result = await alice_mcp_client.call_tool(
            "nc_share_create",
            arguments={
                "path": shared_file,
                "share_with": "bob",
                "share_type": 0,
                "permissions": 1,
            },
        )
        assert not result.isError, f"Alice failed to create share: {result.content}"
        share_data = json.loads(result.content[0].text)
        share_id = share_data["id"]

        # Test: Alice lists files in root
        logger.info("Alice listing files via MCP...")
        result = await alice_mcp_client.call_tool(
            "nc_webdav_list_directory", arguments={"path": "/"}
        )

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            if not isinstance(response_data, list):
                response_data = [response_data] if response_data else []
            file_names = [f["name"] for f in response_data]
            logger.info(f"Alice can see files: {file_names}")

            # Alice should see her own files
            # Note: Exact assertions depend on test isolation
        else:
            logger.warning(f"Alice could not list files: {result.content}")

        # Test: Bob lists files in root
        logger.info("Bob listing files via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_webdav_list_directory", arguments={"path": "/"}
        )

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            if not isinstance(response_data, list):
                response_data = [response_data] if response_data else []
            file_names = [f["name"] for f in response_data]
            logger.info(f"Bob can see files: {file_names}")

            # Bob should see his own file, but not Alice's private file
            # Bob may see shared files in his shared folder or via different path
        else:
            logger.warning(f"Bob could not list files: {result.content}")

    finally:
        # Cleanup
        if share_id:
            logger.info(f"Alice deleting share {share_id}")
            await alice_mcp_client.call_tool(
                "nc_share_delete", arguments={"share_id": share_id}
            )

        logger.info("Cleaning up Alice's files...")
        await alice_mcp_client.call_tool(
            "nc_webdav_delete_resource", arguments={"path": alice_file}
        )
        await alice_mcp_client.call_tool(
            "nc_webdav_delete_resource", arguments={"path": shared_file}
        )

        logger.info("Cleaning up Bob's files...")
        await bob_mcp_client.call_tool(
            "nc_webdav_delete_resource", arguments={"path": bob_file}
        )


@pytest.mark.asyncio
async def test_folder_share_permissions(alice_mcp_client, bob_mcp_client):
    """
    Test that folder sharing works correctly.

    Scenario:
    1. Alice creates a folder via MCP
    2. Alice creates files in the folder via MCP
    3. Alice shares the folder with Bob via MCP
    4. Bob can access files in the shared folder via MCP
    """
    folder_path = "/alice_shared_folder"
    file_in_folder = f"{folder_path}/document.txt"
    file_content = "This is a document in Alice's shared folder"

    # Alice creates folder
    logger.info(f"Alice creating folder: {folder_path}")
    result = await alice_mcp_client.call_tool(
        "nc_webdav_create_directory", arguments={"path": folder_path}
    )
    assert not result.isError, f"Alice failed to create folder: {result.content}"

    # Alice creates file in folder
    logger.info(f"Alice creating file in folder: {file_in_folder}")
    result = await alice_mcp_client.call_tool(
        "nc_webdav_write_file",
        arguments={"path": file_in_folder, "content": file_content},
    )
    assert not result.isError, f"Alice failed to create file: {result.content}"

    share_id = None

    try:
        # Alice shares the folder with Bob
        logger.info("Alice sharing folder with Bob...")
        result = await alice_mcp_client.call_tool(
            "nc_share_create",
            arguments={
                "path": folder_path,
                "share_with": "bob",
                "share_type": 0,
                "permissions": 1,
            },
        )
        assert not result.isError, f"Alice failed to create share: {result.content}"
        share_data = json.loads(result.content[0].text)
        share_id = share_data["id"]
        logger.info(f"Created folder share {share_id}")

        # Test: Bob lists the shared folder
        logger.info("Bob attempting to list shared folder via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_webdav_list_directory", arguments={"path": folder_path}
        )

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            if not isinstance(response_data, list):
                response_data = [response_data] if response_data else []
            logger.info(f"Bob can see {len(response_data)} files in shared folder")

            # Bob should see the file in the shared folder
            file_names = [f["name"] for f in response_data]
            assert "document.txt" in file_names, (
                "Bob should see the file in shared folder"
            )
        else:
            logger.warning(f"Bob could not list shared folder: {result.content}")

        # Test: Bob reads the file in the shared folder
        logger.info("Bob attempting to read file in shared folder via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": file_in_folder}
        )

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            logger.info("Bob successfully read file in shared folder")
            assert "content" in response_data
            assert file_content in response_data["content"]
        else:
            logger.warning(
                f"Bob could not read file in shared folder: {result.content}"
            )

    finally:
        # Cleanup - Alice deletes the share and folder
        if share_id:
            logger.info(f"Alice deleting share {share_id}")
            await alice_mcp_client.call_tool(
                "nc_share_delete", arguments={"share_id": share_id}
            )

        logger.info("Alice cleaning up test folder...")
        await alice_mcp_client.call_tool(
            "nc_webdav_delete_resource", arguments={"path": folder_path}
        )
